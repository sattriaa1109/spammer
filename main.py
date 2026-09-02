import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError

from playwright.async_api import async_playwright, Page
from playwright_stealth import stealth

from database import get_db, AsyncSessionLocal, engine
from models import Base, Account, Target, Log

# ── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("FBAutoLikeAPI")

HEADLESS_MODE  = os.getenv("HEADLESS", "true").lower() == "true"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

app = FastAPI(title="Facebook Auto-Like Bot API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active asyncio tasks: target_id -> asyncio.Task
active_tasks: dict[int, asyncio.Task] = {}


# ── Auto-cleanup ─────────────────────────────────────────────────

async def cleanup_old_logs():
    cutoff = datetime.utcnow() - timedelta(days=7)
    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(delete(Log).where(Log.action_time < cutoff))
            await session.commit()
            logger.info(f"Auto-cleanup: {result.rowcount} old log(s) removed.")
        except SQLAlchemyError as e:
            await session.rollback()
            logger.error(f"Cleanup DB error: {e}")

async def periodic_log_cleanup():
    while True:
        try:
            await asyncio.sleep(86400)
            await cleanup_old_logs()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Periodic cleanup error: {e}")

cleanup_task: Optional[asyncio.Task] = None


# ── Startup / Shutdown ───────────────────────────────────────────

@app.on_event("startup")
async def startup_event():
    global cleanup_task
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables verified/created.")
        await cleanup_old_logs()
        cleanup_task = asyncio.create_task(periodic_log_cleanup())
    except SQLAlchemyError as e:
        logger.critical(f"DB error on startup: {e}")
        raise
    except Exception as e:
        logger.critical(f"Startup error: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    global cleanup_task
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()


# ── Global error handler ─────────────────────────────────────────

@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.error(f"Global DB error handler: {exc}")
    if isinstance(exc, IntegrityError):
        return JSONResponse(status_code=400, content={"detail": "Database integrity error."})
    if isinstance(exc, OperationalError):
        return JSONResponse(status_code=503, content={"detail": "Database connection failure."})
    return JSONResponse(status_code=500, content={"detail": "Internal database error."})


# ── Pydantic Schemas ─────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str

class AccountCreate(BaseModel):
    username: str
    session_cookies: Any
    proxy: Optional[str] = None
    user_agent: Optional[str] = None
    is_active: Optional[bool] = True

class ProxyTestRequest(BaseModel):
    proxy: str

class AutoLikeRequest(BaseModel):
    account_id: int
    target_url: str

class AutoLikeResponse(BaseModel):
    message: str
    target_id: int
    status: str


# ── Bot helpers ──────────────────────────────────────────────────

def parse_proxy(proxy_str: Optional[str]) -> Optional[dict]:
    if not proxy_str:
        return None
    if "://" not in proxy_str:
        proxy_str = f"http://{proxy_str}"
    return {"server": proxy_str}


async def simulate_human_behavior(page: Page):
    delay = random.uniform(5.0, 15.0)
    logger.info(f"Human delay: {delay:.2f}s")
    await asyncio.sleep(delay)
    try:
        await page.evaluate("window.scrollBy(0, window.innerHeight / 2)")
        await asyncio.sleep(random.uniform(2.0, 4.0))
        await page.evaluate("window.scrollBy(0, -100)")
        await asyncio.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        logger.warning(f"Scroll warning: {e}")


async def perform_auto_like(page: Page) -> bool:
    logger.info("Searching for Like/Suka button...")
    like_selectors = [
        page.get_by_role("button", name=re.compile(r"^(suka|like)$", re.IGNORECASE)),
        page.locator('div[aria-label="Suka"][role="button"], div[aria-label="Like"][role="button"]'),
        page.locator('xpath=//div[@role="button" and (contains(@aria-label,"Suka") or contains(@aria-label,"Like"))]'),
        page.locator('xpath=//span[text()="Suka" or text()="Like"]/ancestor::div[@role="button"]'),
    ]
    button_found = None
    for locator in like_selectors:
        try:
            count = await locator.count()
            for i in range(count):
                candidate = locator.nth(i)
                if await candidate.is_visible():
                    button_found = candidate
                    break
            if button_found:
                break
        except Exception as ex:
            logger.debug(f"Locator error: {ex}")
            continue
    if not button_found:
        logger.error("Like/Suka button not found.")
        return False
    await button_found.scroll_into_view_if_needed()
    delay = random.uniform(5.0, 15.0)
    logger.info(f"Pre-click delay: {delay:.2f}s")
    await asyncio.sleep(delay)
    await button_found.click(timeout=10000)
    logger.info("Like button clicked.")
    await asyncio.sleep(random.uniform(3.0, 6.0))
    return True


async def run_auto_like_bot(target_id: int, account_id: int):
    async with AsyncSessionLocal() as session:
        try:
            account = await session.get(Account, account_id)
            if not account or not account.is_active:
                logger.error(f"Account {account_id} inactive/missing.")
                target = await session.get(Target, target_id)
                if target:
                    target.status = "failed"
                    session.add(Log(account_id=account_id, target_id=target_id, message="Account inactive or not found."))
                    await session.commit()
                return

            target = await session.get(Target, target_id)
            if not target:
                return

            target.status = "in_progress"
            await session.commit()
            logger.info(f"Bot starting | Account: {account.username} | URL: {target.url_post}")

            async with async_playwright() as p:
                ctx_kwargs = {}
                if account.user_agent:
                    ctx_kwargs["user_agent"] = account.user_agent
                proxy_cfg = parse_proxy(account.proxy)
                if proxy_cfg:
                    ctx_kwargs["proxy"] = proxy_cfg

                browser = await p.chromium.launch(
                    headless=HEADLESS_MODE,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-infobars", "--disable-dev-shm-usage",
                        "--disable-gpu", "--disable-extensions",
                        "--disable-software-rasterizer", "--no-zygote",
                        "--window-size=1366,768",
                    ],
                )
                context = await browser.new_context(**ctx_kwargs)

                cookies = account.session_cookies
                if cookies:
                    if isinstance(cookies, str):
                        try:
                            cookies = json.loads(cookies)
                        except Exception:
                            pass
                    await context.add_cookies(cookies if isinstance(cookies, list) else [cookies])
                    logger.info("Session cookies loaded.")

                page = await context.new_page()
                await stealth(page)
                await page.route("**/*.{png,jpg,jpeg,gif,webp,mp4,webm}", lambda route: route.abort())

                try:
                    await page.goto(target.url_post, wait_until="domcontentloaded", timeout=60000)
                    await simulate_human_behavior(page)
                    success = await perform_auto_like(page)

                    await session.refresh(target)
                    if target.status == "stopped":
                        logger.info("Task was stopped by user.")
                        return

                    if success:
                        target.status = "success"
                        session.add(Log(account_id=account.id, target_id=target.id, message="Successfully liked the Facebook post."))
                        logger.info("Status → SUCCESS")
                    else:
                        raise Exception("Failed to locate or click the Like button.")

                except asyncio.CancelledError:
                    logger.info(f"Task {target_id} cancelled.")
                    try:
                        await session.rollback()
                        target = await session.get(Target, target_id)
                        if target:
                            target.status = "stopped"
                            session.add(Log(account_id=account.id, target_id=target.id, message="Task stopped/cancelled."))
                            await session.commit()
                    except SQLAlchemyError as db_err:
                        await session.rollback()
                        logger.error(f"DB error during cancel: {db_err}")
                    raise
                except Exception as bot_err:
                    error_msg = str(bot_err)
                    logger.error(f"Bot error: {error_msg}")
                    try:
                        await session.rollback()
                        target = await session.get(Target, target_id)
                        if target and target.status != "stopped":
                            target.status = "failed"
                            session.add(Log(account_id=account.id, target_id=target.id, message=f"Error: {error_msg}"))
                            await session.commit()
                    except SQLAlchemyError as db_err:
                        await session.rollback()
                        logger.error(f"DB error during error handling: {db_err}")
                finally:
                    try:
                        await context.close()
                        await browser.close()
                    except Exception:
                        pass
                    try:
                        await session.commit()
                    except SQLAlchemyError:
                        await session.rollback()

        except asyncio.CancelledError:
            pass
        except SQLAlchemyError as db_exc:
            await session.rollback()
            logger.critical(f"DB error in bot task: {db_exc}")
        except Exception as exc:
            logger.critical(f"Critical bot error: {exc}")
        finally:
            active_tasks.pop(target_id, None)


# ── Authentication ───────────────────────────────────────────────

@app.post("/api/auth/login")
async def admin_login(payload: LoginRequest):
    if payload.password == ADMIN_PASSWORD:
        return {"success": True, "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid password")


# ── Accounts ─────────────────────────────────────────────────────

@app.get("/api/accounts")
async def list_accounts(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Account).order_by(desc(Account.id)))
        return result.scalars().all()
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.post("/api/accounts", status_code=201)
async def create_account(payload: AccountCreate, db: AsyncSession = Depends(get_db)):
    cookies = payload.session_cookies
    if isinstance(cookies, str):
        try:
            cookies = json.loads(cookies)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format for session_cookies.")
    try:
        acc = Account(
            username=payload.username,
            session_cookies=cookies,
            proxy=payload.proxy,
            user_agent=payload.user_agent,
            is_active=payload.is_active if payload.is_active is not None else True,
        )
        db.add(acc)
        await db.commit()
        await db.refresh(acc)
        return {"message": "Account added successfully", "account": acc}
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=f"DB error: {e}")


@app.patch("/api/accounts/{account_id}/toggle")
async def toggle_account(account_id: int, db: AsyncSession = Depends(get_db)):
    try:
        acc = await db.get(Account, account_id)
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found.")
        acc.is_active = not acc.is_active
        await db.commit()
        await db.refresh(acc)
        return {"message": f"is_active={acc.is_active}", "is_active": acc.is_active, "account": acc}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    try:
        acc = await db.get(Account, account_id)
        if not acc:
            raise HTTPException(status_code=404, detail="Account not found.")
        await db.delete(acc)
        await db.commit()
        return {"message": f"Account {account_id} deleted."}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ── Auto-like & Task Control ─────────────────────────────────────

@app.post("/api/auto-like", status_code=202, response_model=AutoLikeResponse)
async def auto_like_endpoint(payload: AutoLikeRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    try:
        acc = await db.get(Account, payload.account_id)
        if not acc or not acc.is_active:
            raise HTTPException(status_code=400, detail="Account does not exist or is inactive.")
        new_target = Target(url_post=payload.target_url, status="pending")
        db.add(new_target)
        await db.commit()
        await db.refresh(new_target)
        task = asyncio.create_task(run_auto_like_bot(target_id=new_target.id, account_id=payload.account_id))
        active_tasks[new_target.id] = task
        return {"message": "Auto-like task successfully started.", "target_id": new_target.id, "status": new_target.status}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.post("/api/targets/{target_id}/stop")
async def stop_target(target_id: int, db: AsyncSession = Depends(get_db)):
    try:
        target = await db.get(Target, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Target not found.")
        if target.status in ["success", "failed", "stopped"]:
            return {"message": f"Task already {target.status}."}
        target.status = "stopped"
        db.add(Log(target_id=target.id, message="Task stopped by user."))
        await db.commit()
        task = active_tasks.pop(target_id, None)
        if task and not task.done():
            task.cancel()
        return {"message": f"Task {target_id} stopped."}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.delete("/api/targets/{target_id}")
async def delete_target(target_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a target and its associated logs. Also cancels any running task."""
    try:
        target = await db.get(Target, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Target not found.")

        # Cancel active task if running
        task = active_tasks.pop(target_id, None)
        if task and not task.done():
            task.cancel()

        # Delete associated logs first (foreign key)
        await db.execute(delete(Log).where(Log.target_id == target_id))
        await db.delete(target)
        await db.commit()
        return {"message": f"Target {target_id} deleted successfully."}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


# ── Monitoring ───────────────────────────────────────────────────

@app.get("/api/targets")
async def list_targets(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Target).order_by(desc(Target.id)).limit(100))
        return result.scalars().all()
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.get("/api/logs")
async def list_logs(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Log).order_by(desc(Log.id)).limit(200))
        return result.scalars().all()
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    try:
        from sqlalchemy import func
        acc_total  = (await db.execute(select(func.count(Account.id)))).scalar() or 0
        acc_active = (await db.execute(select(func.count(Account.id)).where(Account.is_active == True))).scalar() or 0
        t_total    = (await db.execute(select(func.count(Target.id)))).scalar() or 0
        rows = (await db.execute(select(Target.status, func.count(Target.id)).group_by(Target.status))).all()
        breakdown = {r[0]: r[1] for r in rows}
        return {
            "total_accounts":  acc_total,
            "active_accounts": acc_active,
            "total_tasks":     t_total,
            "status_breakdown": {
                "pending":     breakdown.get("pending", 0),
                "in_progress": breakdown.get("in_progress", 0),
                "success":     breakdown.get("success", 0),
                "failed":      breakdown.get("failed", 0),
                "stopped":     breakdown.get("stopped", 0),
            },
        }
    except SQLAlchemyError as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"DB error: {e}")


@app.post("/api/proxy/test")
async def test_proxy(payload: ProxyTestRequest):
    proxy_url = payload.proxy.strip()
    try:
        import urllib.request
        proxy_handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request("https://httpbin.org/ip", headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
            return {"success": True, "message": "Proxy is working!", "details": data}
    except Exception as e:
        return {"success": False, "message": f"Proxy test failed: {str(e)}"}


# ── Serve React Frontend ─────────────────────────────────────────

_dist = os.path.join(os.path.dirname(__file__), "dist")

if os.path.isdir(_dist):
    _assets = os.path.join(_dist, "assets")
    if os.path.isdir(_assets):
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return FileResponse(os.path.join(_dist, "favicon.svg"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def spa_root():
        with open(os.path.join(_dist, "index.html"), encoding="utf-8") as f:
            return f.read()

    @app.get("/{full_path:path}", response_class=HTMLResponse, include_in_schema=False)
    async def spa_catch_all(full_path: str):
        # Let API routes fall through to 404 naturally
        reserved = ("api/", "health", "docs", "openapi.json", "redoc")
        if any(full_path.startswith(r) for r in reserved):
            raise HTTPException(status_code=404)
        with open(os.path.join(_dist, "index.html"), encoding="utf-8") as f:
            return f.read()
    