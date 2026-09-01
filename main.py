import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional, Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status, Request
from fastapi.responses import HTMLResponse, JSONResponse
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


# --- Web Dashboard UI ---
@app.get("/", response_class=HTMLResponse)
async def web_dashboard():
    return """
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Facebook Auto-Like Bot Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen">
    <!-- Login Modal / Screen -->
    <div id="loginModal" class="fixed inset-0 bg-black bg-opacity-80 flex items-center justify-center z-50">
        <div class="bg-gray-800 p-8 rounded-xl shadow-2xl w-full max-w-md border border-gray-700">
            <h2 class="text-2xl font-bold mb-6 text-center text-blue-400"><i class="fa-solid fa-shield-halved mr-2"></i>Admin Login</h2>
            <form id="loginForm" onsubmit="handleLogin(event)" class="space-y-4">
                <div>
                    <label class="block text-sm font-medium mb-1 text-gray-300">Password Admin</label>
                    <input type="password" id="adminPassword" required class="w-full px-4 py-2 bg-gray-900 border border-gray-700 rounded-lg focus:outline-none focus:border-blue-500 text-white" placeholder="Masukkan password...">
                </div>
                <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 font-semibold py-2 rounded-lg transition duration-200">Login</button>
                <p id="loginError" class="text-red-400 text-sm text-center hidden">Password salah!</p>
            </form>
        </div>
    </div>

    <!-- Main Dashboard App -->
    <div id="app" class="hidden container mx-auto px-4 py-8 max-w-7xl">
        <header class="flex justify-between items-center mb-8 bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg">
            <div>
                <h1 class="text-2xl font-extrabold text-blue-400"><i class="fa-brands fa-facebook mr-2"></i>FB Auto-Like Dashboard</h1>
                <p class="text-sm text-gray-400">Manage accounts and automate liking tasks securely</p>
            </div>
            <button onclick="logout()" class="bg-red-600 hover:bg-red-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition"><i class="fa-solid fa-right-from-bracket mr-1"></i>Logout</button>
        </header>

        <div class="grid grid-cols-1 lg:grid-cols-3 gap-8">
            <!-- Left Column: Accounts Management -->
            <div class="lg:col-span-1 space-y-8">
                <!-- Add Account Form -->
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg">
                    <h2 class="text-xl font-bold mb-4 text-green-400"><i class="fa-solid fa-user-plus mr-2"></i>Tambah Akun</h2>
                    <form id="addAccountForm" onsubmit="handleAddAccount(event)" class="space-y-4">
                        <div>
                            <label class="block text-sm font-medium mb-1 text-gray-300">Username / Nama Akun</label>
                            <input type="text" id="accUsername" required class="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-green-500" placeholder="cth: Budi Santoso">
                        </div>
                        <div>
                            <label class="block text-sm font-medium mb-1 text-gray-300">Session Cookies (JSON format)</label>
                            <textarea id="accCookies" rows="3" required class="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-xs font-mono text-white focus:outline-none focus:border-green-500" placeholder='[{"name": "c_user", "value": "123..."}, ...]'></textarea>
                        </div>
                        <div>
                            <label class="block text-sm font-medium mb-1 text-gray-300">Proxy (Opsional)</label>
                            <input type="text" id="accProxy" class="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-green-500" placeholder="http://ip:port">
                        </div>
                        <div>
                            <label class="block text-sm font-medium mb-1 text-gray-300">User Agent (Opsional)</label>
                            <input type="text" id="accUserAgent" class="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-green-500" placeholder="Mozilla/5.0...">
                        </div>
                        <button type="submit" class="w-full bg-green-600 hover:bg-green-700 font-semibold py-2 rounded-lg text-sm transition">Simpan Akun</button>
                    </form>
                </div>

                <!-- Account List -->
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg">
                    <h2 class="text-xl font-bold mb-4 text-blue-400"><i class="fa-solid fa-users mr-2"></i>Daftar Akun</h2>
                    <div id="accountList" class="space-y-3 max-h-96 overflow-y-auto pr-1">
                        <!-- Loaded dynamically -->
                    </div>
                </div>
            </div>

            <!-- Right Column: Auto-Like Control & Tasks -->
            <div class="lg:col-span-2 space-y-8">
                <!-- Start Auto-Like Control Panel -->
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg">
                    <h2 class="text-xl font-bold mb-4 text-purple-400"><i class="fa-solid fa-robot mr-2"></i>Control Panel Auto-Like</h2>
                    <form id="autoLikeForm" onsubmit="handleStartLike(event)" class="space-y-4">
                        <div>
                            <label class="block text-sm font-medium mb-1 text-gray-300">Pilih Akun Aktif</label>
                            <select id="likeAccountSelect" required class="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-purple-500">
                                <!-- Loaded dynamically -->
                            </select>
                        </div>
                        <div>
                            <label class="block text-sm font-medium mb-1 text-gray-300">Target URL Post Facebook</label>
                            <input type="url" id="targetUrl" required class="w-full px-3 py-2 bg-gray-900 border border-gray-700 rounded-lg text-sm text-white focus:outline-none focus:border-purple-500" placeholder="https://www.facebook.com/permalink.php?story_fbid=...">
                        </div>
                        <div class="flex space-x-4">
                            <button type="submit" class="flex-1 bg-purple-600 hover:bg-purple-700 font-bold py-3 rounded-lg text-sm transition flex items-center justify-center shadow-lg"><i class="fa-solid fa-play mr-2"></i>Start Like</button>
                        </div>
                    </form>
                </div>

                <!-- Tasks / Targets Table -->
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg">
                    <div class="flex justify-between items-center mb-4">
                        <h2 class="text-xl font-bold text-yellow-400"><i class="fa-solid fa-tasks mr-2"></i>Riwayat & Status Task</h2>
                        <button onclick="loadData()" class="text-xs bg-gray-700 hover:bg-gray-600 px-3 py-1.5 rounded-lg text-gray-300 transition"><i class="fa-solid fa-rotate mr-1"></i>Refresh</button>
                    </div>
                    <div class="overflow-x-auto">
                        <table class="w-full text-left text-sm text-gray-300">
                            <thead class="bg-gray-900 text-gray-400 uppercase text-xs">
                                <tr>
                                    <th class="p-3">ID</th>
                                    <th class="p-3">URL Target</th>
                                    <th class="p-3">Status</th>
                                    <th class="p-3">Waktu</th>
                                    <th class="p-3 text-center">Aksi</th>
                                </tr>
                            </thead>
                            <tbody id="targetTableBody" class="divide-y divide-gray-700">
                                <!-- Loaded dynamically -->
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Logs Table -->
                <div class="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg">
                    <h2 class="text-xl font-bold mb-4 text-indigo-400"><i class="fa-solid fa-terminal mr-2"></i>Log Aktivitas</h2>
                    <div id="logContainer" class="bg-gray-900 p-4 rounded-lg font-mono text-xs text-green-400 h-48 overflow-y-auto space-y-1">
                        <!-- Loaded dynamically -->
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const AUTH_KEY = "fb_bot_auth";

        window.onload = function() {
            if (localStorage.getItem(AUTH_KEY) === "true") {
                document.getElementById("loginModal").classList.add("hidden");
                document.getElementById("app").classList.remove("hidden");
                loadData();
                setInterval(loadData, 5000); // Auto refresh every 5s
            }
        }

        async function handleLogin(e) {
            e.preventDefault();
            const password = document.getElementById("adminPassword").value;
            try {
                const res = await fetch("/api/auth/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ password })
                });
                if (res.ok) {
                    localStorage.setItem(AUTH_KEY, "true");
                    document.getElementById("loginModal").classList.add("hidden");
                    document.getElementById("app").classList.remove("hidden");
                    loadData();
                } else {
                    document.getElementById("loginError").classList.remove("hidden");
                }
            } catch (err) {
                alert("Login error: " + err);
            }
        }

        function logout() {
            localStorage.removeItem(AUTH_KEY);
            location.reload();
        }

        async function loadData() {
            if (localStorage.getItem(AUTH_KEY) !== "true") return;
            await Promise.all([loadAccounts(), loadTargets(), loadLogs()]);
        }

        async function loadAccounts() {
            try {
                const res = await fetch("/api/accounts");
                const accounts = await res.json();
                
                const listEl = document.getElementById("accountList");
                const selectEl = document.getElementById("likeAccountSelect");
                
                listEl.innerHTML = "";
                selectEl.innerHTML = "";

                if (accounts.length === 0) {
                    listEl.innerHTML = '<p class="text-xs text-gray-500 text-center py-4">Belum ada akun.</p>';
                    selectEl.innerHTML = '<option value="">-- Pilih Akun --</option>';
                    return;
                }

                let selectOptions = "";
                let listHtml = "";

                accounts.forEach(acc => {
                    const statusBadge = acc.is_active 
                        ? '<span class="px-2 py-0.5 bg-green-900 text-green-300 rounded text-xs">Aktif</span>' 
                        : '<span class="px-2 py-0.5 bg-red-900 text-red-300 rounded text-xs">Nonaktif</span>';
                    
                    listHtml += `
                        <div class="bg-gray-900 p-3 rounded-lg border border-gray-700 flex justify-between items-center">
                            <div>
                                <p class="font-semibold text-sm text-white">${acc.username}</p>
                                <p class="text-xs text-gray-400">ID: ${acc.id} | Proxy: ${acc.proxy || 'None'}</p>
                                <div class="mt-1">${statusBadge}</div>
                            </div>
                            <div class="flex space-x-2">
                                <button onclick="toggleAccount(${acc.id})" class="px-2.5 py-1 bg-yellow-600 hover:bg-yellow-700 text-white rounded text-xs transition" title="Toggle Aktif/Nonaktif">
                                    <i class="fa-solid fa-power-off"></i>
                                </button>
                                <button onclick="deleteAccount(${acc.id})" class="px-2.5 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-xs transition" title="Hapus Akun">
                                    <i class="fa-solid fa-trash"></i>
                                </button>
                            </div>
                        </div>
                    `;

                    if (acc.is_active) {
                        selectOptions += `<option value="${acc.id}">${acc.username} (ID: ${acc.id})</option>`;
                    }
                });

                listEl.innerHTML = listHtml;
                selectEl.innerHTML = selectOptions || '<option value="">Tidak ada akun aktif</option>';
            } catch (err) {
                console.error("Failed to load accounts:", err);
            }
        }

        async function handleAddAccount(e) {
            e.preventDefault();
            const username = document.getElementById("accUsername").value;
            let cookiesStr = document.getElementById("accCookies").value;
            const proxy = document.getElementById("accProxy").value || null;
            const user_agent = document.getElementById("accUserAgent").value || null;

            try {
                let session_cookies = JSON.parse(cookiesStr);
                const res = await fetch("/api/accounts", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username, session_cookies, proxy, user_agent })
                });
                if (res.ok) {
                    document.getElementById("addAccountForm").reset();
                    loadAccounts();
                    alert("Akun berhasil ditambahkan!");
                } else {
                    const err = await res.json();
                    alert("Gagal: " + (err.detail || "Unknown error"));
                }
            } catch (err) {
                alert("Format JSON cookies tidak valid!");
            }
        }

        async function toggleAccount(id) {
            try {
                const res = await fetch(`/api/accounts/${id}/toggle`, { method: "PATCH" });
                if (res.ok) {
                    loadAccounts();
                } else {
                    alert("Gagal mengubah status akun.");
                }
            } catch (err) {
                alert("Error: " + err);
            }
        }

        async function deleteAccount(id) {
            if (!confirm("Yakin ingin menghapus akun ini?")) return;
            try {
                const res = await fetch(`/api/accounts/${id}`, { method: "DELETE" });
                if (res.ok) {
                    loadAccounts();
                } else {
                    alert("Gagal menghapus akun.");
                }
            } catch (err) {
                alert("Error: " + err);
            }
        }

        async function handleStartLike(e) {
            e.preventDefault();
            const account_id = parseInt(document.getElementById("likeAccountSelect").value);
            const target_url = document.getElementById("targetUrl").value;

            if (!account_id) {
                alert("Pilih akun aktif terlebih dahulu!");
                return;
            }

            try {
                const res = await fetch("/api/auto-like", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ account_id, target_url })
                });
                if (res.ok) {
                    document.getElementById("targetUrl").value = "";
                    loadTargets();
                    loadLogs();
                    alert("Task auto-like berhasil dimulai!");
                } else {
                    const err = await res.json();
                    alert("Gagal: " + (err.detail || "Unknown error"));
                }
            } catch (err) {
                alert("Error: " + err);
            }
        }

        async function stopTask(targetId) {
            if (!confirm(`Yakin ingin menghentikan task #${targetId}?`)) return;
            try {
                const res = await fetch(`/api/targets/${targetId}/stop`, { method: "POST" });
                if (res.ok) {
                    loadTargets();
                    loadLogs();
                } else {
                    alert("Gagal menghentikan task.");
                }
            } catch (err) {
                alert("Error: " + err);
            }
        }

        async function loadTargets() {
            try {
                const res = await fetch("/api/targets");
                const targets = await res.json();
                const tbody = document.getElementById("targetTableBody");
                tbody.innerHTML = "";

                if (targets.length === 0) {
                    tbody.innerHTML = '<tr><td colspan="5" class="p-4 text-center text-gray-500">Belum ada task.</td></tr>';
                    return;
                }

                targets.forEach(t => {
                    let badge = "";
                    if (t.status === "pending") badge = '<span class="px-2 py-1 bg-yellow-900 text-yellow-300 rounded text-xs">Pending</span>';
                    else if (t.status === "in_progress") badge = '<span class="px-2 py-1 bg-blue-900 text-blue-300 rounded text-xs animate-pulse">In Progress</span>';
                    else if (t.status === "success") badge = '<span class="px-2 py-1 bg-green-900 text-green-300 rounded text-xs">Success</span>';
                    else if (t.status === "failed") badge = '<span class="px-2 py-1 bg-red-900 text-red-300 rounded text-xs">Failed</span>';
                    else if (t.status === "stopped") badge = '<span class="px-2 py-1 bg-gray-700 text-gray-300 rounded text-xs">Stopped</span>';

                    let actionBtn = "";
                    if (t.status === "pending" || t.status === "in_progress") {
                        actionBtn = `<button onclick="stopTask(${t.id})" class="px-2.5 py-1 bg-red-600 hover:bg-red-700 text-white rounded text-xs font-semibold shadow transition"><i class="fa-solid fa-stop mr-1"></i>Stop</button>`;
                    } else {
                        actionBtn = '<span class="text-gray-500 text-xs">-</span>';
                    }

                    tbody.innerHTML += `
                        <tr class="border-b border-gray-700 hover:bg-gray-900">
                            <td class="p-3 font-mono">#${t.id}</td>
                            <td class="p-3 truncate max-w-xs" title="${t.url_post}">${t.url_post}</td>
                            <td class="p-3">${badge}</td>
                            <td class="p-3 text-xs text-gray-400">${new Date(t.created_at).toLocaleString()}</td>
                            <td class="p-3 text-center">${actionBtn}</td>
                        </tr>
                    `;
                });
            } catch (err) {
                console.error("Failed to load targets:", err);
            }
        }

        async function loadLogs() {
            try {
                const res = await fetch("/api/logs");
                const logs = await res.json();
                const container = document.getElementById("logContainer");
                container.innerHTML = "";

                if (logs.length === 0) {
                    container.innerHTML = '<p class="text-gray-500">Tidak ada log aktivitas.</p>';
                    return;
                }

                logs.forEach(l => {
                    container.innerHTML += `<div><span class="text-gray-500">[${new Date(l.action_time).toLocaleTimeString()}]</span> [Account #${l.account_id || 'N/A'} | Target #${l.target_id || 'N/A'}] ${l.message}</div>`;
                });
            } catch (err) {
                console.error("Failed to load logs:", err);
            }
        }
    </script>
</body>
</html>
    """
