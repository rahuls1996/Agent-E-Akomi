import asyncio
import inspect
import traceback
from typing import Annotated

from playwright.async_api import ElementHandle
from playwright.async_api import Page
from playwright.async_api import Locator
from ae.core.playwright_manager import PlaywrightManager
from ae.utils.dom_helper import get_element_outer_html
from ae.utils.dom_mutation_observer import subscribe  # type: ignore
from ae.utils.dom_mutation_observer import unsubscribe  # type: ignore
from ae.utils.logger import logger
from ae.utils.ui_messagetype import MessageType


async def click(selector: Annotated[str, "The properly formed query selector string to identify the element for the click action (e.g. [mmid='114']). When \"mmid\" attribute is present, use it for the query selector."],
                wait_before_execution: Annotated[float, "Optional wait time in seconds before executing the click event logic.", float]) -> Annotated[str, "A message indicating success or failure of the click."]:
    """
    Executes a click action on the element matching the given query selector string within the currently open web page.
    If there is no page open, it will raise a ValueError. An optional wait time can be specified before executing the click logic. Use this to wait for the page to load especially when the last action caused the DOM/Page to load.

    Parameters:
    - selector: The query selector string to identify the element for the click action.
    - wait_before_execution: Optional wait time in seconds before executing the click event logic. Defaults to 0.0 seconds.

    Returns:
    - Success if the click was successful, Appropropriate error message otherwise.
    """
    logger.info(f"Executing ClickElement with \"{selector}\" as the selector")

    # Initialize PlaywrightManager and get the active browser page
    browser_manager = PlaywrightManager(browser_type='chromium', headless=False)
    page = await browser_manager.get_current_page()

    if page is None:
        raise ValueError('No active page found. OpenURL command opens a new page.')

    function_name = inspect.currentframe().f_code.co_name

    await browser_manager.take_screenshots(f"{function_name}_start", page)
    await browser_manager.highlight_element(selector, True)

    dom_changes_detected = None
    def detect_dom_changes(changes: str):
        nonlocal dom_changes_detected
        dom_changes_detected = changes

    subscribe(detect_dom_changes)
    result = await do_click(page, selector, wait_before_execution)
    await asyncio.sleep(0.1)  # sleep for 100ms to allow the mutation observer to detect changes
    unsubscribe(detect_dom_changes)
    await browser_manager.take_screenshots(f"{function_name}_end", page)
    await browser_manager.notify_user(result["summary_message"], message_type=MessageType.ACTION)

    if dom_changes_detected:
        return f"Success: {result['summary_message']}.\nAs a consequence of this action, new elements have appeared in view: {dom_changes_detected}. This means that the action to click {selector} is not yet executed and needs further interaction. Get all_fields DOM to complete the interaction."
    return result["detailed_message"]


async def do_click(page: Page, selector: str, wait_before_execution: float) -> dict[str, str]:
    """
    Executes the click action on the element with the given selector within the provided page.

    Parameters:
    - page: The Playwright page instance.
    - selector: The query selector string to identify the element for the click action.
    - wait_before_execution: Optional wait time inp seconds before executing the click event logic.

    Returns:
    dict[str,str] - Explanation of the outcome of this operation represented as a dictionary with 'summary_message' and 'detailed_message'.
    """
    logger.info(f"Executing ClickElement with \"{selector}\" as the selector. Wait time before execution: {wait_before_execution} seconds.")

    # Wait before execution if specified
    if wait_before_execution > 0:
        await asyncio.sleep(wait_before_execution)

    # Wait for the selector to be present and ensure it's attached and visible. If timeout, try javascript click
    try:
        logger.info(f"Waiting for element \"{selector}\" to be attached and visible.")

        locator = page.locator(selector)
        await locator.wait_for(state='visible', timeout=5000)

        logger.info(f"Scrolling element \"{selector}\" into view.")
        await locator.scroll_into_view_if_needed(timeout=500)

        element_tag_name = await locator.evaluate("element => element.tagName.toLowerCase()")
        element_outer_html = await locator.evaluate("element => element.outerHTML")

        if element_tag_name == "option":
            element_value = await locator.get_attribute("value")
            # Get the parent <select> element
            select_locator = locator.locator('xpath=..')
            # Use select_option on the <select> element
            await select_locator.select_option(value=element_value)
            
            logger.info(f'Select menu option "{element_value}" selected')
            msg = f'Select menu option "{element_value}" selected'
            return {
                "summary_message": msg,
                "detailed_message": f'{msg}. The select element\'s outer HTML is: {element_outer_html}.'
            }

        logger.info(f"Clicking on element \"{selector}\" using Playwright's click method.")
        msg = await perform_playwright_click(locator, selector)
        return {
            "summary_message": msg,
            "detailed_message": f"{msg} The clicked element's outer HTML is: {element_outer_html}."
        }

    except Exception as e:
        logger.error(f"Unable to click element with selector: \"{selector}\". Error: {e}")
        traceback.print_exc()
        msg = f"Unable to click element with selector: \"{selector}\". Error: {e}"
        return {
            "summary_message": msg,
            "detailed_message": msg
        }


async def is_element_present(page: Page, selector: str) -> bool:
    """
    Checks if an element is present on the page.

    Parameters:
    - page: The Playwright page instance.
    - selector: The query selector string to identify the element.

    Returns:
    - True if the element is present, False otherwise.
    """
    element = await page.query_selector(selector)
    return element is not None


async def perform_playwright_click(locator: Locator, selector: str) -> str:
    """
    Performs a click action on the element using Playwright's click method.

    Parameters:
    - locator: The Playwright Locator instance representing the element to be clicked.
    - selector: The query selector string of the element.

    Returns:
    - A message indicating success.
    """
    logger.info(f"Performing Playwright Click on element with selector: {selector}")
    click_options = {"timeout": 1000}
    click_options["force"] = True
    try:
        await locator.click(timeout=1000, force=True)
        logger.info(f"Successfully clicked element with selector: {selector}")
        return f"Successfully clicked element with selector: \"{selector}\"."
    except Exception as e:
        logger.error(f"Error clicking element with selector: {selector}. Error: {e}")
        traceback.print_exc()
        raise e  # Re-raise the exception to be handled by the calling function


async def perform_javascript_click(page: Page, selector: str):
    """
    Performs a click action on the element using JavaScript.

    Parameters:
    - page: The Playwright page instance.
    - selector: The query selector string of the element.

    Returns:
    - None
    """
    js_code = """(selector) => {
        let element = document.querySelector(selector);

        if (!element) {
            console.log(`perform_javascript_click: Element with selector ${selector} not found`);
            return `perform_javascript_click: Element with selector ${selector} not found`;
        }

        if (element.tagName.toLowerCase() === "option") {
            let value = element.text;
            let parent = element.parentElement;

            parent.value = element.value; // Directly set the value if possible
            // Trigger change event if necessary
            let event = new Event('change', { bubbles: true });
            parent.dispatchEvent(event);
            console.log("Chicken Tikka Masala")
            console.log("Select menu option", value, "selected");
            return "Select menu option: "+ value+ " selected";
        }
        else {
            console.log("About to click selector", selector);
            // If the element is a link, make it open in the same tab
            if (element.tagName.toLowerCase() === "a") {
                element.target = "_self";
            }
            let ariaExpandedBeforeClick = element.getAttribute('aria-expanded');
            element.click();
            let ariaExpandedAfterClick = element.getAttribute('aria-expanded');
            if (ariaExpandedBeforeClick === 'false' && ariaExpandedAfterClick === 'true') {
                return "Executed JavaScript Click on element with selector: "+selector +". Very important: As a consequence a menu has appeared where you may need to make further selction. Very important: Get all_fields DOM to complete the action.";
            }
            return "Executed JavaScript Click on element with selector: "+selector;
        }
    }"""
    try:
        logger.info(f"Executing JavaScript click on element with selector: {selector}")
        result:str = await page.evaluate(js_code, selector)
        logger.debug(f"Executed JavaScript Click on element with selector: {selector}")
        return result
    except Exception as e:
        logger.error(f"Error executing JavaScript click on element with selector: {selector}. Error: {e}")
        traceback.print_exc()

