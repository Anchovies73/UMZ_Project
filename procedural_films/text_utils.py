import bpy


def get_active_text_datablock():
    """
    Получает активный текстовый блок из Blender Text Editor.
    Возвращает bpy.types.Text или None.
    """
    try:
        # Попытка найти активный TEXT_EDITOR area
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'TEXT_EDITOR':
                    space = area.spaces.active
                    if hasattr(space, 'text') and space.text:
                        return space.text
    except Exception:
        pass
    return None


def read_active_text():
    """
    Читает содержимое активного текстового блока.
    Возвращает str или None.
    """
    try:
        text_datablock = get_active_text_datablock()
        if text_datablock:
            return text_datablock.as_string()
    except Exception:
        pass
    return None


def write_active_text(content):
    """
    Перезаписывает содержимое активного текстового блока.
    Возвращает True если успешно, False иначе.
    """
    try:
        text_datablock = get_active_text_datablock()
        if text_datablock:
            text_datablock.clear()
            text_datablock.write(content)
            return True
    except Exception:
        pass
    return False
