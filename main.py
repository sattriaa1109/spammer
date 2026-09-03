import asyncio
import json
import logging
import os
import random
import re
from datetime import datetime, timedelta
from typing import Optional, Any, List, Dict
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, delete, func, update
from sqlalchemy.exc import SQLAlchemyError, IntegrityError, OperationalError

from playwright.async_api import async_playwright, Page

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
active_tasks: Dict[int, asyncio.Task] = {}


async def apply_stealth(page: Page):
    """Applies stealth evasion tactics to the Playwright page object."""
    try:
        from playwright_stealth import stealth_async
        await stealth_async(page)
    except (ImportError, TypeError):
        try:
            from playwright_stealth import stealth
            if callable(stealth):
                res = stealth(page)
                if asyncio.iscoroutine(res):
                    await res
            else:
                from playwright_stealth import Stealth
                await Stealth().apply_stealth_async(page)
        except Exception:
            from playwright_stealth import Stealth
            await Stealth().apply_stealth_async(page)


def sanitize_cookies(cookies: Any) -> List[dict]:
    """Sanitizes raw cookie objects from string/JSON/dict into Playwright compatible list of dicts."""
    if isinstance(cookies, str):
        try:
            cookies = json.loads(cookies)
        except Exception:
            return []
    if isinstance(cookies, dict):
        cookies = [cookies]
    if not isinstance(cookies, list):
        return []

    clean_cookies = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        c_clean = dict(c)

        if "name" not in c_clean or "value" not in c_clean:
            continue

        c_clean["name"] = str(c_clean["name"])
        c_clean["value"] = str(c_clean["value"])

        if "domain" not in c_clean and "url" not in c_clean:
            c_clean["domain"] = ".facebook.com"

        if "path" not in c_clean and "url" not in c_clean:
            c_clean["path"] = "/"

        if "sameSite" in c_clean:
            same_site_val = str(c_clean["sameSite"]).capitalize()
            if same_site_val in ["Strict", "Lax", "None"]:
                c_clean["sameSite"] = same_site_val
            elif same_site_val in ["No_restriction", "Unspecified", "None_specified"]:
                c_clean["sameSite"] = "None"
            else:
                c_clean.pop("sameSite", None)

        if "expirationDate" in c_clean:
            try:
                c_clean["expires"] = float(c_clean["expirationDate"])
                c_clean.pop("expirationDate", None)
            except (ValueError, TypeError):
                c_clean.pop("expirationDate", None)

        allowed_keys = {"name", "value", "url", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
        filtered_cookie = {k: v for k, v in c_clean.items() if k in allowed_keys and v is not None}
        clean_cookies.append(filtered_cookie)

    return clean_cookies


def parse_proxy(proxy_str: Optional[str]) -> Optional[dict]:
    """Parses proxy string into Playwright format supporting HTTP/HTTPS/SOCKS5 and Auth."""
    if not proxy_str or not proxy_str.strip():
        return None
    proxy_str = proxy_str.strip()

    # Format: host:port:username:password
    parts = proxy_str.split(":")
    if len(parts) == 4 and not proxy_str.startswith("http"):
        host, port, user, password = parts
        return {
            "server": f"http://{host}:{port}",
            "username": user,
            "password": password
        }

    if "://" not in proxy_str:
        proxy_str = f"http://{proxy_str}"

    try:
        parsed = urlparse(proxy_str)
        server = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            server += f":{parsed.port}"
        config = {"server": server}
        if parsed.username:
            config["username"] = parsed.username
        if parsed.password:
            config["password"] = parsed.password
        return config
    except Exception:
        return {"server": proxy_str}


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


async def reset_stale_pending_tasks():
    """Updates orphaned 'pending' or 'in_progress' tasks on server startup."""
    async with AsyncSessionLocal() as session:
        try:
            stmt = (
                update(Target)
                .where(Target.status.in_(["pending", "in_progress"]))
                .values(status="failed")
            )
            res = await session.execute(stmt)
            if res.rowcount > 0:
                session.add(Log(message=f"Server startup: marked {res.rowcount} interrupted/stale tasks as failed."))
                await session.commit()
                logger.info(f"Marked {res.rowcount} stale tasks as failed.")
        except Exception as e:
            await session.rollback()
            logger.error(f"Error resetting stale tasks: {e}")


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
        
        # Mark interrupted/stale tasks as failed on startup
        await reset_stale_pending_tasks()

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


async def handle_facebook_popups(page: Page):
    """Dismiss cookie consent banners or login modals if present on the page."""
    popup_selectors = [
        'button[data-cookiebanner="accept_button"]',
        'button[title="Allow all cookies"]',
        'button[title="Terima Semua Cookie"]',
        'button:has-text("Allow all cookies")',
        'button:has-text("Terima Semua Cookie")',
        'button:has-text("Izinkan semua cookie")',
        'button:has-text("Decline optional cookies")',
        'div[role="dialog"] button[aria-label="Close"]',
        'div[role="dialog"] button[aria-label="Tutup"]',
        '[aria-label="Decline optional cookies"]',
        '[aria-label="Tutup"]',
        '[aria-label="Close"]',
    ]
    for sel in popup_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                logger.info(f"Dismissing Facebook popup: {sel}")
                await btn.click(timeout=3000)
                await asyncio.sleep(1)
        except Exception:
            pass


async def check_action_block(page: Page):
    """Detect if Facebook has action-blocked or restricted the account from liking."""
    block_indicators = [
        'text="Tindakan Ini Dibatasi"',
        'text="You’re Temporarily Blocked"',
        'text="Action Blocked"',
        'text="You can\'t use this feature right now"',
        'text="Anda tidak dapat menggunakan fitur ini sekarang"',
    ]
    for ind in block_indicators:
        try:
            loc = page.locator(ind)
            if await loc.count() > 0 and await loc.first.is_visible(timeout=800):
                raise Exception("Account Action Blocked: This Facebook account is temporarily restricted from liking posts.")
        except Exception as e:
            if "Action Blocked" in str(e) or "Dibatasi" in str(e):
                raise


async def check_login_status(page: Page):
    """Verifies that the browser session is logged in and not redirected to login page."""
    current_url = page.url.lower()
    if "login" in current_url or "checkpoint" in current_url:
        raise Exception("Account session cookies are invalid or expired (redirected to Facebook login page).")

    try:
        login_form = page.locator('input[name="email"], input[id="email"]')
        if await login_form.count() > 0 and await login_form.first.is_visible(timeout=1000):
            raise Exception("Account session cookies are invalid or expired (login form detected).")
    except Exception as e:
        if "login" in str(e).lower() or "expired" in str(e).lower():
            raise


async def simulate_human_behavior(page: Page):
    """Simulates realistic human browsing behavior with random scrolling and pauses."""
    delay = random.uniform(2.0, 5.0)
    logger.info(f"Applying navigation delay: {delay:.2f} seconds")
    await asyncio.sleep(delay)

    try:
        logger.info("Simulating scrolling behavior...")
        await page.evaluate("window.scrollBy(0, window.innerHeight / 3)")
        await asyncio.sleep(random.uniform(1.5, 3.0))
        await page.evaluate("window.scrollBy(0, -100)")
        await asyncio.sleep(random.uniform(1.0, 2.0))
    except Exception as e:
        logger.warning(f"Scroll simulation warning: {e}")


async def perform_auto_like(page: Page) -> bool:
    """Locates and clicks the Facebook Like/Suka button, supporting Desktop and Mobile FB interfaces."""
    logger.info("Searching for Like/Suka button...")
    await handle_facebook_popups(page)
    await check_action_block(page)

    # 1. Check if post is ALREADY LIKED
    already_liked_selectors = [
        'div[role="button"][aria-label*="Hapus Suka"]',
        'div[role="button"][aria-label*="Batal Suka"]',
        'div[role="button"][aria-label*="Remove Like"]',
        'div[role="button"][aria-label*="Unlike"]',
        'div[role="button"][aria-pressed="true"]',
        'xpath=//div[@role="button" and (contains(@aria-label, "Batal") or contains(@aria-label, "Hapus") or contains(@aria-label, "Remove") or contains(@aria-label, "Unlike"))]',
        'xpath=//span[text()="Disukai" or text()="Liked"]',
        'a[href*="/a/unlike.php"]',
        'input[type="submit"][value*="Batal Suka"]',
        'input[type="submit"][value*="Unlike"]',
    ]

    for sel in already_liked_selectors:
        try:
            loc = page.locator(sel)
            if await loc.count() > 0 and await loc.first.is_visible(timeout=1000):
                logger.info("Post is already liked by this account.")
                return True
        except Exception:
            pass

    # 2. Search for UNLIKED Like/Suka button (Desktop & Mobile selectors)
    like_selectors = [
        'div[role="button"][aria-label="Suka"]',
        'div[role="button"][aria-label="Like"]',
        'div[role="button"][aria-label*="Suka"]',
        'div[role="button"][aria-label*="Like"]',
        'xpath=//div[@role="button" and (contains(@aria-label, "Suka") or contains(@aria-label, "Like"))]',
        'xpath=//span[(text()="Suka" or text()="Like")]/ancestor::div[@role="button"]',
        'xpath=//span[contains(text(), "Suka") or contains(text(), "Like")]/ancestor::div[@role="button"]',
        'a[href*="/a/like.php"]',
        'a[href*="like.php"]',
        'input[type="submit"][value="Suka"]',
        'input[type="submit"][value="Like"]',
        'button:has-text("Suka")',
        'button:has-text("Like")',
    ]

    button_found = None
    for selector in like_selectors:
        try:
            loc = page.locator(selector)
            count = await loc.count()
            for i in range(count):
                candidate = loc.nth(i)
                if await candidate.is_visible(timeout=1000):
                    # Exclude comment / share buttons if selector is broad
                    label = (await candidate.get_attribute("aria-label") or "").lower()
                    if "komentar" in label or "comment" in label or "bagikan" in label or "share" in label:
                        continue
                    button_found = candidate
                    break
            if button_found:
                break
        except Exception as ex:
            logger.debug(f"Selector '{selector}' check error: {ex}")
            continue

    # Fallback to get_by_role without restrictive anchors
    if not button_found:
        try:
            role_btn = page.get_by_role("button", name=re.compile(r"(suka|like)", re.IGNORECASE))
            count = await role_btn.count()
            for i in range(count):
                candidate = role_btn.nth(i)
                if await candidate.is_visible(timeout=1000):
                    button_found = candidate
                    break
        except Exception:
            pass

    if not button_found:
        logger.error("Like/Suka button not found on the page.")
        return False

    try:
        await button_found.scroll_into_view_if_needed()
        pre_click_delay = random.uniform(1.5, 3.0)
        logger.info(f"Waiting {pre_click_delay:.2f}s before clicking Like button...")
        await asyncio.sleep(pre_click_delay)

        # Force click or dispatch event to bypass reaction hover panels
        try:
            await button_found.click(timeout=5000, force=True)
        except Exception:
            await button_found.dispatch_event("click")

        logger.info("Successfully clicked the Like/Suka button.")
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # Check for Facebook action block prompt
        await check_action_block(page)
        return True

    except Exception as err:
        if "Action Blocked" in str(err) or "Dibatasi" in str(err):
            raise
        logger.error(f"Standard click failed ({err}), attempting JavaScript click fallback...")
        try:
            await button_found.evaluate("el => el.click()")
            logger.info("JS click dispatched successfully.")
            await asyncio.sleep(random.uniform(2.0, 4.0))
            await check_action_block(page)
            return True
        except Exception as js_err:
            if "Action Blocked" in str(js_err) or "Dibatasi" in str(js_err):
                raise
            logger.error(f"JS click fallback failed: {js_err}")
            return False


async def run_auto_like_bot(target_id: int, account_id: int):
    """Background task executing the Playwright automation bot."""
    async with AsyncSessionLocal() as session:
        try:
            # 1. Fetch Account and Target
            account = await session.get(Account, account_id)
            target = await session.get(Target, target_id)

            if not target:
                logger.error(f"Target ID {target_id} not found in database.")
                return

            if not account or not account.is_active:
                msg = f"Account ID {account_id} not found or is inactive."
                logger.error(msg)
                target.status = "failed"
                session.add(Log(account_id=account_id if account else None, target_id=target_id, message=msg))
                await session.commit()
                return

            # Mark status as in_progress immediately
            target.status = "in_progress"
            await session.commit()
            logger.info(f"Task {target_id} status updated to IN_PROGRESS for account: {account.username}")

            # 2. Initialize Playwright Browser Context
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

                # Sanitize and load session cookies
                clean_cookies = sanitize_cookies(account.session_cookies)
                if clean_cookies:
                    try:
                        await context.add_cookies(clean_cookies)
                        logger.info(f"Loaded {len(clean_cookies)} session cookies into browser context.")
                    except Exception as cookie_err:
                        logger.warning(f"Error setting session cookies: {cookie_err}")
                else:
                    logger.warning("No valid cookies found for account.")

                page = await context.new_page()
                await apply_stealth(page)

                # Block heavy media resources asynchronously
                async def block_media(route):
                    if route.request.resource_type in ["media", "font"]:
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", block_media)

                try:
                    logger.info(f"Navigating to Target URL: {target.url_post}")
                    await page.goto(target.url_post, wait_until="domcontentloaded", timeout=60000)

                    # Check for login redirection or login prompt
                    await check_login_status(page)

                    # Simulate browsing behavior
                    await simulate_human_behavior(page)

                    # Perform auto-like action
                    success = await perform_auto_like(page)

                    # Mobile Fallback Retry: If Desktop FB failed to locate/click button, retry via mbasic.facebook.com
                    if not success and "www.facebook.com" in target.url_post:
                        mobile_url = target.url_post.replace("www.facebook.com", "mbasic.facebook.com")
                        logger.info(f"Retrying auto-like via mobile interface: {mobile_url}")
                        try:
                            await page.goto(mobile_url, wait_until="domcontentloaded", timeout=30000)
                            await check_login_status(page)
                            await handle_facebook_popups(page)
                            success = await perform_auto_like(page)
                        except Exception as mob_err:
                            logger.warning(f"Mobile interface retry warning: {mob_err}")

                    # Re-verify task cancellation state
                    await session.refresh(target)
                    if target.status == "stopped":
                        logger.info(f"Task {target_id} was stopped by user during execution.")
                        return

                    if success:
                        target.status = "success"
                        session.add(Log(
                            account_id=account.id,
                            target_id=target.id,
                            message="Successfully liked the Facebook post."
                        ))
                        await session.commit()
                        logger.info(f"Task {target_id} completed successfully (SUCCESS).")
                    else:
                        raise Exception("Failed to locate or click the Like button on both Desktop and Mobile interfaces.")

                except asyncio.CancelledError:
                    logger.info(f"Task {target_id} received cancellation request.")
                    target.status = "stopped"
                    session.add(Log(account_id=account.id, target_id=target.id, message="Task stopped/cancelled by user."))
                    await session.commit()
                    raise
                except Exception as bot_err:
                    error_msg = str(bot_err)
                    logger.error(f"Automation error for target {target_id}: {error_msg}")
                    target.status = "failed"
                    session.add(Log(
                        account_id=account.id,
                        target_id=target.id,
                        message=f"Error: {error_msg}"
                    ))
                    await session.commit()
                    
                finally:
                    try:
                        await context.close()
                        await browser.close()
                    except Exception:
                        pass

        except asyncio.CancelledError:
            logger.info(f"Task {target_id} process cancelled.")
            async with AsyncSessionLocal() as rollback_session:
                try:
                    t = await rollback_session.get(Target, target_id)
                    if t and t.status != "stopped":
                        t.status = "stopped"
                        rollback_session.add(Log(account_id=account_id, target_id=target_id, message="Task stopped/cancelled."))
                        await rollback_session.commit()
                except Exception:
                    pass
        except Exception as exc:
            logger.critical(f"Critical unhandled error in background task for target {target_id}: {exc}")
            async with AsyncSessionLocal() as err_session:
                try:
                    t = await err_session.get(Target, target_id)
                    if t and t.status not in ["success", "stopped"]:
                        t.status = "failed"
                        err_session.add(Log(account_id=account_id, target_id=target_id, message=f"Critical error: {str(exc)}"))
                        await err_session.commit()
                except Exception:
                    pass
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

        # Launch background task and track it
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
