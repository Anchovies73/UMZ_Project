import bpy


def get_active_text_datablock():
    """
    Iterate through open windows/screens/areas to find a TEXT_EDITOR area
    and return its active space's text datablock (bpy.types.Text).
    
    Returns:
        bpy.types.Text or None: The active text datablock if found, otherwise None.
    """
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "TEXT_EDITOR":
                    space = area.spaces.active
                    if hasattr(space, "text") and space.text:
                        return space.text
    except Exception:
        pass
    return None


def read_active_text():
    """
    Returns the content of the active text datablock as a string.
    
    Returns:
        str or None: The text content if an active text datablock exists, otherwise None.
    """
    try:
        txt = get_active_text_datablock()
        if txt:
            return txt.as_string()
    except Exception:
        pass
    return None


def write_active_text(content):
    """
    Overwrite the CURRENT active text datablock with the provided content.
    
    Args:
        content (str): The content to write to the active text datablock.
    
    Returns:
        bool: True on success, False otherwise.
    """
    try:
        txt = get_active_text_datablock()
        if txt:
            txt.clear()
            txt.write(content)
            return True
    except Exception:
        pass
    return False
