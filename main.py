import asyncio
import logging
import os
import random
import re
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

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

app = FastAPI(title="Facebook Auto-Like Bot API", version="1.0.0")


@app.on_event("startup")
async def startup_event():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables verified/created successfully.")


# Pydantic Schemas
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
                target = await session.get(Target, target_id)
                if target:
                    target.status = "failed"
                    session.add(Log(account_id=account_id, target_id=target_id, message="Account inactive or not found."))
                    await session.commit()
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
                        import json
                        cookies = json.loads(cookies)
                    await context.add_cookies(cookies)
                    logger.info(f"Loaded {len(cookies)} cookies into browser context.")

                page = await context.new_page()
                await stealth(page)

                # Block heavy media/images to save RAM and bandwidth
                await page.route("**/*.{png,jpg,jpeg,gif,webp,mp4,webm}", lambda route: route.abort())

                try:
                    logger.info(f"Navigating to URL: {target.url_post}")
                    await page.goto(target.url_post, wait_until="domcontentloaded", timeout=60000)

                    await simulate_human_behavior(page)
                    success = await perform_auto_like(page)

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

                except Exception as bot_err:
                    error_msg = str(bot_err)
                    logger.error(f"Automation error: {error_msg}")
                    target.status = "failed"
                    session.add(Log(
                        account_id=account.id, 
                        target_id=target.id, 
                        message=f"Error: {error_msg}"
                    ))
                
                finally:
                    await context.close()
                    await browser.close()
                    await session.commit()

        except Exception as exc:
            logger.critical(f"Critical error in background task: {exc}")


@app.post("/api/auto-like", status_code=status.HTTP_202_ACCEPTED, response_model=AutoLikeResponse)
async def auto_like_endpoint(payload: AutoLikeRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """API Endpoint to queue an auto-like task on a Facebook post."""
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

    background_tasks.add_task(run_auto_like_bot, target_id=new_target.id, account_id=payload.account_id)

    return {
        "message": "Auto-like task successfully queued.",
        "target_id": new_target.id,
        "status": new_target.status
    }


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    return {"status": "healthy"}
