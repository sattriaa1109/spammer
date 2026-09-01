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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError

from playwright.async_api import async_playwright, Page
from playwright_stealth import stealth

from database import get_db, AsyncSessionLocal, engine
from models import Base, Account, Target, Log

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("FBAutoLikeAPI")

HEADLESS_MODE = os.getenv("HEADLESS", "true").lower() == "true"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

app = FastAPI(title="Facebook Auto-Like Bot API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Track active background tasks: target_id -> asyncio.Task
active_tasks: dict[int, asyncio.Task] = {}


async def cleanup_old_logs():
    """Deletes logs older than 7 days from the database."""
    cutoff_date = datetime.utcnow() - timedelta(days=7)
    async with AsyncSessionLocal() as session:
        try:
            stmt = delete(Log).where(Log.action_time < cutoff_date)
            result = await session.execute(stmt)
            await session.commit()
            logger.info(f"Auto-cleanup: Removed logs older than 7 days (cutoff: {cutoff_date}). Rows deleted: {result.rowcount}")
        except SQLAlchemyError as e:
            await session.rollback()
            logger.error(f"Database error during log auto-cleanup: {e}")
        except Exception as e:
            await session.rollback()
            logger.error(f"Unexpected error during log auto-cleanup: {e}")


async def periodic_log_cleanup():
    """Periodically runs log auto-cleanup every 24 hours."""
    while True:
        try:
            await asyncio.sleep(86400) # 24 hours
            await cleanup_old_logs()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in periodic log cleanup: {e}")


cleanup_task: Optional[asyncio.Task] = None


@app.on_event("startup")
async def startup_event():
    global cleanup_task
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables verified/created successfully.")
        
        # Run log cleanup on startup
        await cleanup_old_logs()
        
        # Start background periodic cleanup task
        cleanup_task = asyncio.create_task(periodic_log_cleanup())
    except SQLAlchemyError as e:
        logger.critical(f"Database error during startup: {e}")
        raise
    except Exception as e:
        logger.critical(f"Unexpected error during startup: {e}")
        raise


@app.on_event("shutdown")
async def shutdown_event():
    global cleanup_task
    if cleanup_task and not cleanup_task.done():
        cleanup_task.cancel()


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.error(f"Database error intercepted by global handler: {exc}")
    if isinstance(exc, IntegrityError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Database integrity error (e.g., duplicate entry or foreign key violation)."}
        )
    elif isinstance(exc, OperationalError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Database operational error or connection failure."}
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An internal database error occurred."}
    )


# Pydantic Schemas
class LoginRequest(BaseModel):
    password: str


class AccountCreate(BaseModel):
    username: str
    session_cookies: Any  # Can be dict, list, or JSON string
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


def parse_proxy(proxy_str: Optional[str]) -> Optional[dict]:
    if not proxy_str:
        return None
    if "://" not in proxy_str:
        proxy_str = f"http://{proxy_str}"
    return {"server": proxy_str}


async def simulate_human_behavior(page: Page):
    delay = random.uniform(5.0, 15.0)
    logger.info(f"Applying random navigation delay: {delay:.2f} seconds")
    await asyncio.sleep(delay)

    try:
        logger.info("Simulating scrolling behavior...")
        await page.evaluate("window.scrollBy(0, window.innerHeight / 2)")
        await asyncio.sleep(random.uniform(2.0, 4.0))
        await page.evaluate("window.scrollBy(0, -100)")
        await asyncio.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        logger.warning(f"Scroll simulation warning: {e}")


async def perform_auto_like(page: Page) -> bool:
    logger.info("Searching for Like/Suka button...")
    
    like_selectors = [
        page.get_by_role("button", name=re.compile(r"^(suka|like)$", re.IGNORECASE)),
        page.locator('div[aria-label="Suka"][role="button"], div[aria-label="Like"][role="button"]'),
        page.locator('xpath=//div[@role="button" and (contains(@aria-label, "Suka") or contains(@aria-label, "Like"))]'),
        page.locator('xpath=//span[text()="Suka" or text()="Like"]/ancestor::div[@role="button"]'),
    ]

    button_found = None
    for locator in like_selectors:
        try:
            count = await locator.count()
            if count > 0:
                for i in range(count):
                    candidate = locator.nth(i)
                    if await candidate.is_visible():
                        button_found = candidate
                        break
            if button_found:
                break
        except Exception as ex:
            logger.debug(f"Locator check error: {ex}")
            continue

    if not button_found:
        logger.error("Like/Suka button not found on the page.")
        return False

    await button_found.scroll_into_view_if_needed()
    pre_click_delay = random.uniform(5.0, 15.0)
    logger.info(f"Waiting {pre_click_delay:.2f}s before clicking Like button...")
    await asyncio.sleep(pre_click_delay)

    await button_found.click(timeout=10000)
    logger.info("Successfully clicked the Like/Suka button.")
    
    await asyncio.sleep(random.uniform(3.0, 6.0))
    return True


async def run_auto_like_bot(target_id: int, account_id: int):
    """Background task executing the Playwright automation bot."""
    async with AsyncSessionLocal() as session:
        try:
            account = await session.get(Account, account_id)
            if not account or not account.is_active:
                logger.error(f"Account ID {account_id} not found or inactive.")
                try:
                    target = await session.get(Target, target_id)
                    if target:
                        target.status = "failed"
                        session.add(Log(account_id=account_id, target_id=target_id, message="Account inactive or not found."))
                        await session.commit()
                except SQLAlchemyError as db_err:
                    await session.rollback()
                    logger.error(f"Database error updating target status: {db_err}")
                return

            target = await session.get(Target, target_id)
            if not target:
                logger.error(f"Target ID {target_id} not found.")
                return

            target.status = "in_progress"
            await session.commit()

            logger.info(f"Starting bot for Account: {account.username} | Target URL: {target.url_post}")

            async with async_playwright() as p:
                context_kwargs = {}
                if account.user_agent:
                    context_kwargs["user_agent"] = account.user_agent
                
                proxy_config = parse_proxy(account.proxy)
                if proxy_config:
                    context_kwargs["proxy"] = proxy_config

                browser = await p.chromium.launch(
                    headless=HEADLESS_MODE,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-infobars",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-extensions",
                        "--disable-software-rasterizer",
                        "--no-zygote",
                        "--window-size=1366,768",
                    ],
                )
                
                context = await browser.new_context(**context_kwargs)

                cookies = account.session_cookies
                if cookies:
                    if isinstance(cookies, str):
                        try:
                            cookies = json.loads(cookies)
                        except Exception:
                            pass
                    await context.add_cookies(cookies if isinstance(cookies, list) else [cookies])
                    logger.info("Loaded session cookies into browser context.")

                page = await context.new_page()
                await stealth(page)

                # Block heavy media/images to save RAM and bandwidth
                await page.route("**/*.{png,jpg,jpeg,gif,webp,mp4,webm}", lambda route: route.abort())

                try:
                    logger.info(f"Navigating to URL: {target.url_post}")
                    await page.goto(target.url_post, wait_until="domcontentloaded", timeout=60000)

                    await simulate_human_behavior(page)
                    success = await perform_auto_like(page)

                    # Check if stopped during execution
                    await session.refresh(target)
                    if target.status == "stopped":
                        logger.info("Task was stopped by user.")
                        return

                    if success:
                        target.status = "success"
                        session.add(Log(
                            account_id=account.id, 
                            target_id=target.id, 
                            message="Successfully liked the Facebook post."
                        ))
                        logger.info("Target status updated to: SUCCESS")
                    else:
                        raise Exception("Failed to locate or click the Like button.")

                except asyncio.CancelledError:
                    logger.info(f"Task for target {target_id} was cancelled.")
                    try:
                        await session.rollback()
                        target = await session.get(Target, target_id)
                        if target:
                            target.status = "stopped"
                            session.add(Log(account_id=account.id, target_id=target.id, message="Task stopped/cancelled."))
                            await session.commit()
                    except SQLAlchemyError as db_err:
                        await session.rollback()
                        logger.error(f"Database error during cancellation handling: {db_err}")
                    raise
                except Exception as bot_err:
                    error_msg = str(bot_err)
                    logger.error(f"Automation error: {error_msg}")
                    try:
                        await session.rollback()
                        target = await session.get(Target, target_id)
                        if target and target.status != "stopped":
                            target.status = "failed"
                            session.add(Log(
                                account_id=account.id, 
                                target_id=target.id, 
                                message=f"Error: {error_msg}"
                            ))
                            await session.commit()
                    except SQLAlchemyError as db_err:
                        await session.rollback()
                        logger.error(f"Database error during error handling: {db_err}")
                
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
            logger.critical(f"Database error in background task: {db_exc}")
        except Exception as exc:
            logger.critical(f"Critical error in background task: {exc}")
        finally:
            if target_id in active_tasks:
                del active_tasks[target_id]


# --- API Endpoints ---

@app.post("/api/auth/login", status_code=status.HTTP_200_OK)
async def admin_login(payload: LoginRequest):
    if payload.password == ADMIN_PASSWORD:
        return {"success": True, "message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid password")


@app.get("/api/accounts", status_code=status.HTTP_200_OK)
async def list_accounts(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Account).order_by(desc(Account.id)))
        accounts = result.scalars().all()
        return accounts
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in list_accounts: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while listing accounts.")


@app.post("/api/accounts", status_code=status.HTTP_201_CREATED)
async def create_account(payload: AccountCreate, db: AsyncSession = Depends(get_db)):
    cookies = payload.session_cookies
    if isinstance(cookies, str):
        try:
            cookies = json.loads(cookies)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON format for session_cookies.")

    try:
        new_account = Account(
            username=payload.username,
            session_cookies=cookies,
            proxy=payload.proxy,
            user_agent=payload.user_agent,
            is_active=payload.is_active if payload.is_active is not None else True
        )
        db.add(new_account)
        await db.commit()
        await db.refresh(new_account)
        return {"message": "Account added successfully", "account": new_account}
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in create_account: {e}")
        raise HTTPException(status_code=400, detail=f"Database error: {str(e)}")


@app.delete("/api/accounts/{account_id}", status_code=status.HTTP_200_OK)
async def delete_account(account_id: int, db: AsyncSession = Depends(get_db)):
    try:
        account = await db.get(Account, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found.")
        await db.delete(account)
        await db.commit()
        return {"message": f"Account {account_id} deleted successfully."}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in delete_account: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while deleting account.")


@app.patch("/api/accounts/{account_id}/toggle", status_code=status.HTTP_200_OK)
async def toggle_account(account_id: int, db: AsyncSession = Depends(get_db)):
    try:
        account = await db.get(Account, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found.")
        account.is_active = not account.is_active
        await db.commit()
        await db.refresh(account)
        return {"message": f"Account status updated to active={account.is_active}", "account": account}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in toggle_account: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while toggling account.")


@app.post("/api/auto-like", status_code=status.HTTP_202_ACCEPTED, response_model=AutoLikeResponse)
async def auto_like_endpoint(payload: AutoLikeRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """API Endpoint to queue an auto-like task on a Facebook post."""
    try:
        account = await db.get(Account, payload.account_id)
        if not account or not account.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Account does not exist or is inactive."
            )

        new_target = Target(url_post=payload.target_url, status="pending")
        db.add(new_target)
        await db.commit()
        await db.refresh(new_target)

        task = asyncio.create_task(run_auto_like_bot(target_id=new_target.id, account_id=payload.account_id))
        active_tasks[new_target.id] = task

        return {
            "message": "Auto-like task successfully started.",
            "target_id": new_target.id,
            "status": new_target.status
        }
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in auto_like_endpoint: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while starting auto-like task.")


@app.post("/api/targets/{target_id}/stop", status_code=status.HTTP_200_OK)
async def stop_target(target_id: int, db: AsyncSession = Depends(get_db)):
    """Stop an active/pending auto-like task."""
    try:
        target = await db.get(Target, target_id)
        if not target:
            raise HTTPException(status_code=404, detail="Target not found.")

        if target.status in ["success", "failed", "stopped"]:
            return {"message": f"Target task is already {target.status}."}

        target.status = "stopped"
        db.add(Log(target_id=target.id, message="Task stopped by user."))
        await db.commit()

        if target_id in active_tasks:
            task = active_tasks[target_id]
            if not task.done():
                task.cancel()
            del active_tasks[target_id]

        return {"message": f"Target task {target_id} stopped successfully."}
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in stop_target: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while stopping target task.")


@app.get("/api/targets", status_code=status.HTTP_200_OK)
async def list_targets(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Target).order_by(desc(Target.id)).limit(50))
        return result.scalars().all()
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in list_targets: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while listing targets.")


@app.get("/api/logs", status_code=status.HTTP_200_OK)
async def list_logs(db: AsyncSession = Depends(get_db)):
    try:
        result = await db.execute(select(Log).order_by(desc(Log.id)).limit(100))
        return result.scalars().all()
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in list_logs: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while listing logs.")


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    return {"status": "healthy"}


@app.post("/api/proxy/test", status_code=status.HTTP_200_OK)
async def test_proxy(payload: ProxyTestRequest):
    proxy_url = payload.proxy.strip()
    try:
        import urllib.request
        proxy_handler = urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request("https://httpbin.org/ip", headers={"User-Agent": "Mozilla/5.0"})
        with opener.open(req, timeout=8) as response:
            data = response.read().decode('utf-8')
            return {"success": True, "message": "Proxy is working!", "details": json.loads(data)}
    except Exception as e:
        return {"success": False, "message": f"Proxy test failed: {str(e)}"}


@app.get("/api/stats", status_code=status.HTTP_200_OK)
async def get_stats(db: AsyncSession = Depends(get_db)):
    try:
        from sqlalchemy import func
        acc_count = await db.execute(select(func.count(Account.id)))
        active_acc_count = await db.execute(select(func.count(Account.id)).where(Account.is_active == True))
        target_count = await db.execute(select(func.count(Target.id)))
        
        status_counts = await db.execute(
            select(Target.status, func.count(Target.id)).group_by(Target.status)
        )
        status_dict = {row[0]: row[1] for row in status_counts.all()}
        
        return {
            "total_accounts": acc_count.scalar() or 0,
            "active_accounts": active_acc_count.scalar() or 0,
            "total_tasks": target_count.scalar() or 0,
            "status_breakdown": {
                "pending": status_dict.get("pending", 0),
                "in_progress": status_dict.get("in_progress", 0),
                "success": status_dict.get("success", 0),
                "failed": status_dict.get("failed", 0),
                "stopped": status_dict.get("stopped", 0),
            }
        }
    except SQLAlchemyError as e:
        await db.rollback()
        logger.error(f"Database error in get_stats: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred while fetching stats.")



@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Facebook Auto-Like Bot API",
        "version": "1.1.0",
        "docs": "/docs"
    }
