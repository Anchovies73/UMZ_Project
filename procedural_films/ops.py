import bpy
import os
from datetime import datetime

from .storage import (
    read_internal_films,
    write_internal_films,
    write_animation_to_file,
    remove_animation_file,
    read_all_films_cached,
    read_external_films,
    mark_cache_dirty,
    get_external_folder,
)
from .blender_codec import (
    serialize_nla_for_object,
    deserialize_nla_for_object,
    deserialize_action,
    pushdown_action_to_nla,
    nla_has_transform_curves,
)
from .three_export import (
    build_three_clip_from_saved_entry,
    write_three_animation_to_file,
    _get_active_text_datablock,
)


def create_animation_entry(name, description=""):
    return {"created_at": datetime.now().isoformat(), "description": description, "tracks": []}


def _capture_timeline_markers(scene):
    """
    Capture timeline markers from the scene.
    Returns a list of dicts with 'name' and 'frame' keys, or None if no markers.
    """
    try:
        markers = scene.timeline_markers
        if not markers or len(markers) == 0:
            return None
        
        markers_data = []
        for marker in markers:
            markers_data.append({
                "name": marker.name,
                "frame": int(marker.frame)
            })
        return markers_data
    except Exception:
        return None


def _restore_timeline_markers(scene, markers_data):
    """
    Restore timeline markers to the scene.
    markers_data should be a list of dicts with 'name' and 'frame' keys.
    """
    if not markers_data:
        return
    
    try:
        # Clear existing markers
        scene.timeline_markers.clear()
        
        # Add saved markers
        for marker_info in markers_data:
            name = marker_info.get("name", "")
            frame = marker_info.get("frame")
            if frame is not None:
                marker = scene.timeline_markers.new(name, frame=int(frame))
    except Exception:
        pass


def _capture_text_editor_content():
    """
    Capture the content of the active text editor.
    Returns a dict with 'name' and 'content' keys, or None if no active text editor.
    """
    try:
        text_datablock = _get_active_text_datablock()
        if not text_datablock:
            return None
        
        return {
            "name": text_datablock.name,
            "content": text_datablock.as_string()
        }
    except Exception:
        return None


def _restore_text_editor_content(text_data):
    """
    Restore text editor content.
    text_data should be a dict with 'name' and 'content' keys.
    """
    if not text_data:
        return
    
    try:
        name = text_data.get("name")
        content = text_data.get("content")
        
        # Skip if name is missing; allow empty content as it's a valid state
        if not name or content is None:
            return
        
        # Get or create text datablock
        text_datablock = bpy.data.texts.get(name)
        if not text_datablock:
            text_datablock = bpy.data.texts.new(name)
        
        # Set content
        text_datablock.clear()
        text_datablock.write(content)
    except Exception:
        pass


def _clear_animation_on_object(obj):
    try:
        if obj.animation_data:
            try:
                obj.animation_data.action = None
            except Exception:
                pass
            try:
                for t in list(obj.animation_data.nla_tracks):
                    try:
                        obj.animation_data.nla_tracks.remove(t)
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                obj.animation_data_clear()
            except Exception:
                try:
                    obj.animation_data.action = None
                except Exception:
                    pass
    except Exception:
        pass


def create_animation_from_scene(name, description="", only_selected=False):
    internal = read_internal_films()
    entry = create_animation_entry(name, description)
    
    if only_selected:
        objs = list(bpy.context.selected_objects)
        entry["visible_objects_mode"] = "SELECTED"
        entry["visible_objects"] = [o.name for o in objs]
    
    else:
        objs = list(bpy.data.objects)
        entry["visible_objects_mode"] = "ALL"
        if "visible_objects" in entry:
            del entry["visible_objects"]    

    for obj in objs:
        nla_struct = serialize_nla_for_object(obj)
        if nla_struct and nla_has_transform_curves(nla_struct):
            entry["tracks"].append({"object_name": obj.name, "animation": nla_struct})

    try:
        entry["frame_start"] = int(bpy.context.scene.frame_start)
        entry["frame_end"] = int(bpy.context.scene.frame_end)
    except Exception:
        pass

    # Capture timeline markers (optional)
    markers_data = _capture_timeline_markers(bpy.context.scene)
    if markers_data:
        entry["timeline_markers"] = markers_data

    # Capture text editor content (optional)
    text_data = _capture_text_editor_content()
    if text_data:
        entry["text_editor"] = text_data

    internal[name] = entry
    write_internal_films(internal)
    write_animation_to_file(name, entry)

    # three_<name>.json
    try:
        three_clip = build_three_clip_from_saved_entry(name, entry)
        folder = get_external_folder()
        ok = write_three_animation_to_file(name, three_clip, folder)
        if not ok:
            print("[three-export] write_three_animation_to_file вернул False (папка не задана?)")
    except Exception as e:
        print("[three-export ERROR]", repr(e))
        import traceback
        traceback.print_exc()

    mark_cache_dirty()
    return True


def update_animation_from_scene(anim_name, only_selected=False):
    internal = read_internal_films()
    if anim_name not in internal:
        raise RuntimeError("Анимация не найдена.")

    entry = internal[anim_name]
    new_tracks = []
    
    if only_selected:
        objs = list(bpy.context.selected_objects)
        entry["visible_objects_mode"] = "SELECTED"
        entry["visible_objects"] = [o.name for o in objs]
    
    else:
        objs = list(bpy.data.objects)
        entry["visible_objects_mode"] = "ALL"
        if "visible_objects" in entry:
            del entry["visible_objects"]  

    for obj in objs:
        nla_struct = serialize_nla_for_object(obj)
        if nla_struct and nla_has_transform_curves(nla_struct):
            new_tracks.append({"object_name": obj.name, "animation": nla_struct})

    entry["tracks"] = new_tracks
    entry["created_at"] = datetime.now().isoformat()

    try:
        entry["frame_start"] = int(bpy.context.scene.frame_start)
        entry["frame_end"] = int(bpy.context.scene.frame_end)
    except Exception:
        pass

    # Capture timeline markers (optional)
    markers_data = _capture_timeline_markers(bpy.context.scene)
    if markers_data:
        entry["timeline_markers"] = markers_data
    else:
        # Remove if there are no markers anymore
        entry.pop("timeline_markers", None)

    # Capture text editor content (optional)
    text_data = _capture_text_editor_content()
    if text_data:
        entry["text_editor"] = text_data
    else:
        # Remove if there's no text editor content anymore
        entry.pop("text_editor", None)

    internal[anim_name] = entry
    write_internal_films(internal)
    write_animation_to_file(anim_name, entry)

    # three_<name>.json
    try:
        three_clip = build_three_clip_from_saved_entry(anim_name, entry)
        folder = get_external_folder()
        ok = write_three_animation_to_file(anim_name, three_clip, folder)
        if not ok:
            print("[three-export] write_three_animation_to_file вернул False (папка не задана?)")
    except Exception as e:
        print("[three-export ERROR]", repr(e))
        import traceback
        traceback.print_exc()

    mark_cache_dirty()
    return True


def delete_animation(anim_name, full_delete=False):
    internal = read_internal_films()
    entry = internal.get(anim_name)
    if not entry:
        ext = read_external_films()
        entry = ext.get(anim_name)

    # полное удаление: чистим NLA и удаляем связанные Actions
    action_names = set()
    if full_delete and entry:
        for tr in entry.get("tracks", []):
            anim = tr.get("animation", {}) or {}
            for t in anim.get("tracks", []):
                for s in t.get("strips", []):
                    act = s.get("action")
                    if isinstance(act, dict):
                        n = act.get("name")
                        if n:
                            action_names.add(n)

        for a_name in list(action_names):
            for obj in bpy.data.objects:
                ad = getattr(obj, "animation_data", None)
                if not ad:
                    continue
                try:
                    if ad.action and ad.action.name == a_name:
                        ad.action = None
                except Exception:
                    pass
                try:
                    for track in list(ad.nla_tracks):
                        for strip in list(track.strips):
                            try:
                                if strip.action and strip.action.name == a_name:
                                    track.strips.remove(strip)
                            except Exception:
                                pass
                        try:
                            if len(track.strips) == 0:
                                ad.nla_tracks.remove(track)
                        except Exception:
                            pass
                except Exception:
                    pass

        for a_name in list(action_names):
            a = bpy.data.actions.get(a_name)
            if a:
                try:
                    bpy.data.actions.remove(a)
                except Exception:
                    pass

    removed = False
    if anim_name in internal:
        del internal[anim_name]
        write_internal_films(internal)
        removed = True

    remove_animation_file(anim_name)

    # удалить three_<name>.json
    folder = get_external_folder()
    if folder:
        try:
            p = os.path.join(folder, f"three_{anim_name}.json")
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass

    mark_cache_dirty()

    names = list(read_all_films_cached().keys())
    try:
        bpy.context.scene.umz_selected_animation = names[0] if names else ""
    except Exception:
        pass

    return removed

def _apply_visibility_from_entry(entry):
    mode = entry.get("visible_objects_mode", "ALL")

    if mode != "SELECTED":
        for obj in bpy.data.objects:
            try:
                obj.hide_set(False)
            except Exception:
                pass
            try:
                obj.hide_render = False
            except Exception:
                pass
        return

    visible = entry.get("visible_objects") or []
    visible_set = set([v for v in visible if isinstance(v, str)])

    for obj in bpy.data.objects:
        show = (obj.name in visible_set)
        try:
            obj.hide_set(not show)
        except Exception:
            pass
        try:
            obj.hide_render = (not show)
        except Exception:
            pass

def apply_animation_to_scene(anim_name, remove_other_animations=True):
    scene = bpy.context.scene
    all_films = read_all_films_cached()
    film = all_films.get(anim_name)
    if not film:
        raise RuntimeError("Анимация не найдена.")

    # Apply saved frame range if present
    try:
        frame_range_updated = False
        if "frame_start" in film:
            frame_start = int(film["frame_start"])
            scene.frame_start = frame_start
            frame_range_updated = True
        if "frame_end" in film:
            frame_end = int(film["frame_end"])
            scene.frame_end = frame_end
            frame_range_updated = True
        
        # Clamp current frame to new range if needed
        if frame_range_updated:
            current = scene.frame_current
            start = scene.frame_start
            end = scene.frame_end
            if current < start:
                scene.frame_set(start)
            elif current > end:
                scene.frame_set(end)
    except (ValueError, TypeError):
        # Invalid frame range values, skip applying them
        pass

    track_objs = {t.get("object_name") for t in film.get("tracks", [])}

    if remove_other_animations:
        for obj in bpy.data.objects:
            if obj.name not in track_objs:
                _clear_animation_on_object(obj)

    applied = []

    for tr in film.get("tracks", []):
        obj_name = tr.get("object_name")
        anim_struct = tr.get("animation", {}) or {}
        obj = bpy.data.objects.get(obj_name)
        if not obj:
            continue

        nla_tracks_data = anim_struct.get("tracks", [])
        if nla_tracks_data and len(nla_tracks_data) > 0:
            if not obj.animation_data:
                obj.animation_data_create()
            created_actions, saved_active = deserialize_nla_for_object(obj, anim_struct)
            if saved_active:
                a = bpy.data.actions.get(saved_active)
                if a and obj.animation_data:
                    try:
                        obj.animation_data.action = a
                    except Exception:
                        pass
            else:
                try:
                    if obj.animation_data:
                        obj.animation_data.action = None
                except Exception:
                    pass
        else:
            action_data = anim_struct.get("action")
            if action_data:
                action_obj = deserialize_action(action_data, prefer_name=f"{obj.name}__{action_data.get('name')}")
                if action_obj:
                    if not obj.animation_data:
                        obj.animation_data_create()
                    pushdown_action_to_nla(obj, action_obj, start_frame=None)
                    if anim_struct.get("active_action_name"):
                        try:
                            obj.animation_data.action = action_obj
                        except Exception:
                            pass
                    else:
                        try:
                            obj.animation_data.action = None
                        except Exception:
                            pass

        applied.append(obj_name)

    try:
        current_frame = scene.frame_current
        scene.frame_set(current_frame)
        bpy.context.view_layer.update()
    except Exception:
        pass
    
    # Restore timeline markers if present
    if "timeline_markers" in film:
        _restore_timeline_markers(scene, film["timeline_markers"])
    
    # Restore text editor content if present
    if "text_editor" in film:
        _restore_text_editor_content(film["text_editor"])
    
    _apply_visibility_from_entry(film)
    return {"applied": applied}