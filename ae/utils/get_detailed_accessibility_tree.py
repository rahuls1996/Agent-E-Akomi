import json
import os
import re
import traceback
from typing import Annotated
from typing import Any

from playwright.async_api import Page

from ae.config import SOURCE_LOG_FOLDER_PATH
from ae.core.playwright_manager import PlaywrightManager
from ae.utils.logger import logger

space_delimited_mmid: re.Pattern[str] = re.compile(r'^[\d ]+$')

def is_space_delimited_mmid(s: str) -> bool:
    """
    Check if the given string matches the the mmid pattern of number space repeated.

    Parameters:
    - s (str): The string to check against the pattern.

    Returns:
    - bool: True if the string matches the pattern, False otherwise.
    """
    # Use fullmatch() to ensure the entire string matches the pattern
    return bool(space_delimited_mmid.fullmatch(s))


async def __inject_attributes(page: Page):
    """
    Injects 'mmid' and 'aria-keyshortcuts' into all DOM elements, including dynamically loaded ones.
    """
    # First, inject attributes into existing elements
    last_mmid = await page.evaluate("""() => {
        const allElements = document.querySelectorAll('*');
        let id = 0;
        allElements.forEach(element => {
            const origAriaAttribute = element.getAttribute('aria-keyshortcuts');
            const mmid = `${++id}`;
            element.setAttribute('mmid', mmid);
            element.setAttribute('aria-keyshortcuts', mmid);
            if (origAriaAttribute) {
                element.setAttribute('orig-aria-keyshortcuts', origAriaAttribute);
            }
        });
        return id;
    }""")
    logger.debug(f"Added MMID into {last_mmid} elements")


async def __fetch_dom_info(page, accessibility_tree, only_input_fields: bool):
    # Define the attributes to fetch for each element
    attributes = ['name', 'aria-label', 'placeholder', 'mmid', "id", "for", "data-testid"]
    backup_attributes = []  # If the attributes are not found, then try to get these attributes
    tags_to_ignore = ['head', 'style', 'script', 'link', 'meta', 'noscript', 'template', 'iframe', 'g', 'main', 'c-wiz', 'svg', 'path']
    attributes_to_delete = ["level", "multiline", "haspopup", "id", "for"]
    ids_to_ignore = ['agentDriveAutoOverlay']

    # Recursive function to process each node in the accessibility tree
    async def process_node(node):
        if 'children' in node:
            for child in node['children']:
                await process_node(child)

        mmid_temp = node.get('keyshortcuts') or node.get('description')

        if mmid_temp and is_space_delimited_mmid(mmid_temp):
            mmid_temp = mmid_temp.split(' ')[-1]
        else:
            mmid_temp = None

        if not mmid_temp and node.get('name'):
            # Attempt to find the mmid by querying the DOM
            mmid_temp = await page.evaluate('''(text) => {
                const elements = Array.from(document.querySelectorAll('li[mmid], span[mmid], div[mmid]'));
                const element = elements.find(el => el.textContent.trim() === text);
                return element ? element.getAttribute('mmid') : null;
            }''', node['name'])

        if mmid_temp and is_space_delimited_mmid(mmid_temp):
            mmid_temp = mmid_temp.split(' ')[-1]

        try:
            mmid = int(mmid_temp)
        except (ValueError, TypeError):
            return  # Skip processing this node if mmid is not found

        if node.get('role') == 'dialog' and node.get('modal') == True:  # noqa: E712
            node["important information"] = "This is a modal dialog. Please interact with this dialog and close it to be able to interact with the full page (e.g., by pressing the close button or selecting an option)."

        if mmid:
            # Determine if we need to fetch 'innerText' based on the absence of 'children' in the accessibility node
            should_fetch_inner_text = 'children' not in node

            js_code = """
            (input_params) => {
                const should_fetch_inner_text = input_params.should_fetch_inner_text;
                const mmid = input_params.mmid;
                const attributes = input_params.attributes;
                const tags_to_ignore = input_params.tags_to_ignore;
                const ids_to_ignore = input_params.ids_to_ignore;

                const element = document.querySelector(`[mmid="${mmid}"]`);

                if (!element) {
                    console.log(`No element found with mmid: ${mmid}`);
                    return null;
                }

                if (ids_to_ignore.includes(element.id)) {
                    console.log(`Ignoring element with id: ${element.id}`, element);
                    return null;
                }

                if (tags_to_ignore.includes(element.tagName.toLowerCase()) || element.tagName.toLowerCase() === "option") return null;

                let attributes_to_values = {
                    'tag': element.tagName.toLowerCase(),
                    'mmid': mmid
                };

                if (element.tagName.toLowerCase() === 'input') {
                    attributes_to_values['tag_type'] = element.type;
                }

                for (const attribute of attributes) {
                    let value = element.getAttribute(attribute);

                    if (value) {
                        attributes_to_values[attribute] = value;
                    }
                }

                if (should_fetch_inner_text && element.innerText) {
                    attributes_to_values['description'] = element.innerText;
                }

                return attributes_to_values;
            }
            """

            # Fetch attributes and possibly 'innerText' from the DOM element by 'mmid'
            element_attributes = await page.evaluate(js_code,
                                                     {"mmid": mmid, "attributes": attributes, "backup_attributes": backup_attributes,
                                                      "should_fetch_inner_text": should_fetch_inner_text,
                                                      "tags_to_ignore": tags_to_ignore,
                                                      "ids_to_ignore": ids_to_ignore})

            if 'keyshortcuts' in node:
                del node['keyshortcuts']  # Remove keyshortcuts since it is not needed

            node["mmid"] = str(mmid)

            # Update the node with fetched information
            if element_attributes:
                node.update(element_attributes)

                # Remove redundant or unnecessary attributes
                if node.get('name') == node.get('mmid') and node.get('role') != "textbox":
                    del node['name']  # Remove 'name' from the node

                if 'name' in node and 'description' in node and (node['name'] == node['description'] or node['name'] == node['description'].replace('\n', ' ') or node['description'].replace('\n', '') in node['name']):
                    del node['description']

                if 'name' in node and 'aria-label' in node and node['aria-label'] in node['name']:
                    del node['aria-label']

                if 'name' in node and 'text' in node and node['name'] == node['text']:
                    del node['text']

                if node.get('tag') == "select":
                    node.pop("children", None)
                    node.pop("role", None)
                    node.pop("description", None)

                if node.get('role') == node.get('tag'):
                    del node['role']

                if node.get("aria-label") and node.get("placeholder") and node["aria-label"] == node["placeholder"]:
                    del node["aria-label"]

                if node.get("role") == "link":
                    del node["role"]
                    if node.get("description"):
                        node["text"] = node["description"]
                        del node["description"]

                for attribute_to_delete in attributes_to_delete:
                    if attribute_to_delete in node:
                        node.pop(attribute_to_delete, None)
            else:
                node["marked_for_deletion_by_mm"] = True  # Mark node for deletion if attributes are not found

    await process_node(accessibility_tree)

    pruned_tree = __prune_tree(accessibility_tree, only_input_fields)

    return pruned_tree


async def __cleanup_dom(page: Page):
    """
    Cleans up the DOM by removing injected 'aria-description' attributes and restoring any original 'aria-keyshortcuts'
    from 'orig-aria-keyshortcuts'.
    """
    logger.debug("Cleaning up the DOM's previous injections")
    await page.evaluate("""() => {
        const allElements = document.querySelectorAll('*[mmid]');
        allElements.forEach(element => {
            element.removeAttribute('aria-keyshortcuts');
            const origAriaLabel = element.getAttribute('orig-aria-keyshortcuts');
            if (origAriaLabel) {
                element.setAttribute('aria-keyshortcuts', origAriaLabel);
                element.removeAttribute('orig-aria-keyshortcuts');
            }
        });
    }""")
    logger.debug("DOM cleanup complete")


def __prune_tree(node: dict[str, Any], only_input_fields: bool) -> dict[str, Any] | None:
    """
    Recursively prunes a tree starting from `node`, based on pruning conditions and handling of 'unraveling'.

    The function has two main jobs:
    1. Pruning: Remove nodes that don't meet certain conditions, like being marked for deletion.
    2. Unraveling: For nodes marked with 'marked_for_unravel_children', we replace them with their children,
       effectively removing the node and lifting its children up a level in the tree.

    This happens in place, meaning we modify the tree as we go, which is efficient but means you should
    be cautious about modifying the tree outside this function during a prune operation.

    Args:
    - node (Dict[str, Any]): The node we're currently looking at. We'll check this node, its children,
      and so on, recursively down the tree.
    - only_input_fields (bool): If True, we're only interested in pruning input-related nodes (like form fields).
      This lets you narrow the focus if, for example, you're only interested in cleaning up form-related parts
      of a larger tree.

    Returns:
    - dict[str, Any] | None: The pruned version of `node`, or None if `node` was pruned away. When we 'unravel'
      a node, we directly replace it with its children in the parent's list of children, so the return value
      will be the parent, updated in place.

    Notes:
    - 'marked_for_deletion_by_mm' is our flag for nodes that should definitely be removed.
    - Unraveling is neat for flattening the tree when a node is just a wrapper without semantic meaning.
    - We use a while loop with manual index management to safely modify the list of children as we iterate over it.
    """
    if "marked_for_deletion_by_mm" in node:
        return None

    if 'children' in node:
        i = 0
        while i < len(node['children']):
            child = node['children'][i]
            if 'marked_for_unravel_children' in child:
                # Replace the current child with its children
                if 'children' in child:
                    node['children'] = node['children'][:i] + child['children'] + node['children'][i+1:]
                    i += len(child['children']) - 1  # Adjust the index for the new children
                else:
                    # If the node marked for unraveling has no children, remove it
                    node['children'].pop(i)
                    i -= 1  # Adjust the index since we removed an element
            else:
                # Recursively prune the child if it's not marked for unraveling
                pruned_child = __prune_tree(child, only_input_fields)
                if pruned_child is None:
                    # If the child is pruned, remove it from the children list
                    node['children'].pop(i)
                    i -= 1  # Adjust the index since we removed an element
                else:
                    # Update the child with the pruned version
                    node['children'][i] = pruned_child
            i += 1  # Move to the next child

        # After processing all children, if the children array is empty, remove it
        if not node['children']:
            del node['children']

    # Apply existing conditions to decide if the current node should be pruned
    return None if __should_prune_node(node, only_input_fields) else node


def __should_prune_node(node: dict[str, Any], only_input_fields: bool):
    """
    Determines if a node should be pruned based on its 'role' and 'element_attributes'.

    Args:
        node (dict[str, Any]): The node to be evaluated.
        only_input_fields (bool): Flag indicating whether only input fields should be considered.

    Returns:
        bool: True if the node should be pruned, False otherwise.
    """
    #If the request is for only input fields and this is not an input field, then mark the node for prunning
    if node.get("role") != "WebArea" and only_input_fields and not (node.get("tag") in ("input", "button", "textarea") or node.get("role") == "button"):
        return True

    if node.get('role') == 'generic' and 'children' not in node and not ('name' in node and node.get('name')):  # The presence of 'children' is checked after potentially deleting it above
        return True

    if node.get('role') in ['separator', 'LineBreak']:
        return True
    processed_name = ""
    if 'name' in node:
        processed_name:str =node.get('name') # type: ignore
        processed_name = processed_name.replace(',', '')
        processed_name = processed_name.replace(':', '')
        processed_name = processed_name.replace('\n', '')
        processed_name = processed_name.strip()
        if len(processed_name) <3:
            processed_name = ""

    #check if the node only have name and role, then delete that node
    if len(node) == 2 and 'name' in node and 'role' in node and not (node.get('role') == "text" and processed_name != ""):
        return True
    return False

async def get_node_dom_element(page: Page, mmid: str):
    return await page.evaluate("""
        (mmid) => {
            return document.querySelector(`[mmid="${mmid}"]`);
        }
    """, mmid)


async def get_element_attributes(page: Page, mmid: str, attributes: list[str]):
    return await page.evaluate("""
        (inputParams) => {
            const mmid = inputParams.mmid;
            const attributes = inputParams.attributes;
            const element = document.querySelector(`[mmid="${mmid}"]`);
            if (!element) return null;  // Return null if element is not found

            let attrs = {};
            for (let attr of attributes) {
                attrs[attr] = element.getAttribute(attr);
            }
            return attrs;
        }
    """, {"mmid": mmid, "attributes": attributes})


async def get_dom_with_accessibility_info() -> Annotated[dict[str, Any] | None, "A minified representation of the HTML DOM for the current webpage"]:
    """
    Retrieves, processes, and minifies the Accessibility tree of the active page in a browser instance.
    Strictly follow the name and role tag for any interaction with the nodes.

    Returns:
    - The minified JSON content of the browser's active page.
    """
    logger.debug("Executing Get Accessibility Tree Command")
    # Create and use the PlaywrightManager
    browser_manager = PlaywrightManager(browser_type='chromium', headless=False)
    page = await browser_manager.get_current_page()
    if page is None: # type: ignore
        raise ValueError('No active page found')

    return await do_get_accessibility_info(page)


async def do_get_accessibility_info(page: Page, only_input_fields: bool = False):
    """
    Retrieves the accessibility information of a web page and saves it as JSON files.

    Args:
        page (Page): The page object representing the web page.
        only_input_fields (bool, optional): If True, only retrieves accessibility information for input fields.
            Defaults to False.

    Returns:
        dict[str, Any] or None: The enhanced accessibility tree as a dictionary, or None if an error occurred.
    """
    await __inject_attributes(page)
    accessibility_tree: dict[str, Any] = await page.accessibility.snapshot(interesting_only=True)  # type: ignore

    with open(os.path.join(SOURCE_LOG_FOLDER_PATH, 'json_accessibility_dom.json'), 'w',  encoding='utf-8') as f:
        f.write(json.dumps(accessibility_tree, indent=2))
        logger.debug("json_accessibility_dom.json saved")

    await __cleanup_dom(page)
    try:
        enhanced_tree = await __fetch_dom_info(page, accessibility_tree, only_input_fields)

        logger.debug("Enhanced Accessibility Tree ready")

        with open(os.path.join(SOURCE_LOG_FOLDER_PATH, 'json_accessibility_dom_enriched.json'), 'w',  encoding='utf-8') as f:
            f.write(json.dumps(enhanced_tree, indent=2))
            logger.debug("json_accessibility_dom_enriched.json saved")

        return enhanced_tree
    except Exception as e:
        logger.error(f"Error while fetching DOM info: {e}")
        traceback.print_exc()
        return None
