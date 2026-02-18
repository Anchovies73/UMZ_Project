import bpy
import json
import os

from mathutils import Quaternion
from .constants import (
    ROT_BAKE_STEP_FRAMES,
    CAMERA_BAKE_EVERY_FRAME,
    CAMERA_BAKE_STEP_FRAMES,
    GLTF_ID_PROP,
)
from .text_utils import get_active_text_datablock

# =========================================================
# ЭКСПОРТ В THREE.JS (entry -> three_<name>.json)
# Здесь НЕТ логики хранения библиотеки (Text/внешние name.json).
# Мы принимаем entry (словарь) и строим clip для three.js.
# =========================================================

# -------------------------
# Базовые хелперы времени
# -------------------------

def _get_scene_fps(scene):
    try:
        fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
        return fps if fps > 0 else 24.0
    except Exception:
        return 24.0


def _frame_to_time(frame, frame_start, fps):
    return float(frame - frame_start) / float(fps)


# -------------------------
# Идентификаторы/камера
# -------------------------

def _safe_node_id(obj):
    """
    Возвращает node id для three clip track.
    Сначала пробует obj[GLTF_ID_PROP], иначе obj.name.
    """
    try:
        v = obj.get(GLTF_ID_PROP)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return obj.name


def _is_camera_object(obj):
    try:
        if obj and getattr(obj, "type", None) == "CAMERA":
            return True
    except Exception:
        pass
    try:
        return bool(obj and isinstance(obj.name, str) and obj.name.startswith("Camera"))
    except Exception:
        return False


# -------------------------
# Ключи/кадры из сериализованного entry (NLA)
# -------------------------

def _collect_keyframes(action_fcurves, data_path, array_index, frame_start, frame_end):
    """
    Возвращает отсортированный список (frame, value) для указанного канала.
    action_fcurves может быть:
      - реальный fcurve объект Blender, либо
      - сериализованный dict (как у нас в entry)
    """
    for fc in action_fcurves:
        if hasattr(fc, "data_path"):
            dp = fc.data_path
            idx = int(getattr(fc, "array_index", -1))
        else:
            dp = fc.get("data_path")
            idx = int(fc.get("array_index", -1))

        if dp == data_path and idx == int(array_index):
            pts = []

            # Blender fcurve
            if hasattr(fc, "keyframe_points"):
                for kp in fc.keyframe_points:
                    try:
                        fr = float(kp.co.x)
                        val = float(kp.co.y)
                    except Exception:
                        continue
                    if fr < frame_start or fr > frame_end:
                        continue
                    pts.append((fr, val))

            # сериализованный dict
            else:
                for kp in fc.get("keyframes", []):
                    co = kp.get("co")
                    if not co or len(co) < 2:
                        continue
                    fr = float(co[0])
                    val = float(co[1])
                    if fr < frame_start or fr > frame_end:
                        continue
                    pts.append((fr, val))

            pts.sort(key=lambda x: x[0])
            return pts

    return []


def _union_frames(*channels):
    frames = set()
    for ch in channels:
        for fr, _ in ch:
            frames.add(int(round(float(fr))))
    return sorted(frames)


def _collect_nla_keyframes(anim, data_path, array_index, frame_start, frame_end):
    """
    Собирает ключи из сериализованных NLA strips и мапит Action frames -> Scene frames.
    Возвращает список (scene_frame, value).
    """
    pts = []

    for nla_track in (anim.get("tracks") or []):
        for strip in (nla_track.get("strips") or []):
            if strip.get("muted"):
                continue

            act = strip.get("action") or {}
            fcurves = act.get("fcurves") or []
            if not fcurves:
                continue

            # ключи в action-space
            ch = _collect_keyframes(fcurves, data_path, array_index, -10**12, 10**12)
            if not ch:
                continue

            s_frame_start = float(strip.get("frame_start", 0.0) or 0.0)
            a_frame_start = float(strip.get("action_frame_start", 0.0) or 0.0)
            s_scale = float(strip.get("scale", 1.0) or 1.0)

            # базовый маппинг (reverse/repeat пока не поддержаны)
            for a_fr, val in ch:
                scene_fr = s_frame_start + (float(a_fr) - a_frame_start) * s_scale
                scene_fr = int(round(scene_fr))
                if scene_fr < frame_start or scene_fr > frame_end:
                    continue
                pts.append((scene_fr, float(val)))

    pts.sort(key=lambda x: x[0])
    return pts


def _collect_nla_keyframes_frames(anim, data_path, array_index, frame_start, frame_end):
    """
    Собирает только кадры ключей (без значений) из сериализованных NLA strips.
    Возвращает sorted list[int] кадры сцены.
    """
    frames = set()

    for nla_track in (anim.get("tracks") or []):
        for strip in (nla_track.get("strips") or []):
            if strip.get("muted"):
                continue

            act = strip.get("action") or {}
            fcurves = act.get("fcurves") or []
            if not fcurves:
                continue

            ch = _collect_keyframes(fcurves, data_path, array_index, -10**12, 10**12)
            if not ch:
                continue

            s_frame_start = float(strip.get("frame_start", 0.0) or 0.0)
            a_frame_start = float(strip.get("action_frame_start", 0.0) or 0.0)
            s_scale = float(strip.get("scale", 1.0) or 1.0)

            for a_fr, _val in ch:
                scene_fr = s_frame_start + (float(a_fr) - a_frame_start) * s_scale
                scene_fr = int(round(scene_fr))
                if scene_fr < frame_start or scene_fr > frame_end:
                    continue
                frames.add(scene_fr)

    return sorted(frames)


def _build_number_track(obj_name, prop_path, frames, frame_start, fps, get_value):
    times = [_frame_to_time(fr, frame_start, fps) for fr in frames]
    values = [float(get_value(fr)) for fr in frames]
    return {"type": "number", "name": f"{obj_name}.{prop_path}", "times": times, "values": values}


# -------------------------
# alpha_tracks (ручной runtime в three.js)
# -------------------------

def _collect_nla_number_track(anim, data_path, array_index, frame_start, frame_end, fps):
    """
    Строит number track (times/values) из сериализованных NLA для 1 канала.
    Дедуп по кадру сцены (последний wins).
    """
    frame_to_value = {}

    for nla_track in (anim.get("tracks") or []):
        for strip in (nla_track.get("strips") or []):
            if strip.get("muted"):
                continue

            act = strip.get("action") or {}
            fcurves = act.get("fcurves") or []
            if not fcurves:
                continue

            ch = _collect_keyframes(fcurves, data_path, array_index, -10**12, 10**12)
            if not ch:
                continue

            s_frame_start = float(strip.get("frame_start", 0.0) or 0.0)
            a_frame_start = float(strip.get("action_frame_start", 0.0) or 0.0)
            s_scale = float(strip.get("scale", 1.0) or 1.0)

            for a_fr, val in ch:
                scene_fr = s_frame_start + (float(a_fr) - a_frame_start) * s_scale
                scene_fr = int(round(scene_fr))
                if scene_fr < frame_start or scene_fr > frame_end:
                    continue
                frame_to_value[scene_fr] = float(val)

    if not frame_to_value:
        return None

    frames = sorted(frame_to_value.keys())
    times = [_frame_to_time(fr, frame_start, fps) for fr in frames]
    values = [float(frame_to_value[fr]) for fr in frames]
    return {"frames": frames, "times": times, "values": values}


def _build_alpha_tracks_for_object(node_id, anim, frame_start, frame_end, fps):
    """
    Возвращает dict alpha_track или None.
    Приоритет:
      1) Custom Property ["alpha"] index=0
      2) Object Color alpha: data_path="color" index=3
    """
    # 1) CP ["alpha"]
    t = _collect_nla_number_track(anim, '["alpha"]', 0, frame_start, frame_end, fps)
    if t:
        vals = [max(0.0, min(1.0, v)) for v in t["values"]]
        return {"node": node_id, "times": t["times"], "values": vals, "source": '["alpha"]'}

    # 2) Object.color[3]
    t = _collect_nla_number_track(anim, "color", 3, frame_start, frame_end, fps)
    if t:
        vals = [max(0.0, min(1.0, v)) for v in t["values"]]
        return {"node": node_id, "times": t["times"], "values": vals, "source": "color[3]"}

    return None


# -------------------------
# Timeline markers and text editor helpers
# -------------------------

def _parse_text_blocks(text_content):
    """
    Разбивает текст на блоки по пустым строкам.
    Возвращает список строк (блоков), каждый блок - текст с сохранением внутренних переносов.
    """
    if not text_content:
        return []
    
    blocks = []
    current_block = []
    
    for line in text_content.split('\n'):
        stripped = line.strip()
        if not stripped:
            # Пустая строка - завершаем текущий блок
            if current_block:
                block_text = '\n'.join(current_block)
                block_text = block_text.strip()
                if block_text:
                    blocks.append(block_text)
                current_block = []
        else:
            # Добавляем строку в текущий блок
            current_block.append(line)
    
    # Добавляем последний блок, если он есть
    if current_block:
        block_text = '\n'.join(current_block)
        block_text = block_text.strip()
        if block_text:
            blocks.append(block_text)
    
    return blocks


def _parse_id_and_text(block):
    """
    Парсит блок текста, извлекая id токен и остальной текст.
    id токен - это последовательность цифр, разделенных точками, заканчивающаяся точкой.
    Примеры: '1.', '1.1.', '1.2.1.1.'
    
    Возвращает (id, text) или (None, None) если формат неверный.
    """
    if not block:
        return None, None
    
    # Ищем первый пробел
    space_idx = block.find(' ')
    
    if space_idx == -1:
        # Нет пробела - весь блок должен быть id токеном
        token = block
        text = ''
    else:
        token = block[:space_idx]
        text = block[space_idx + 1:]
    
    # Проверяем формат id токена
    if not token.endswith('.'):
        return None, None
    
    # Проверяем, что до точки только цифры и точки
    parts = token[:-1].split('.')
    if not parts:
        return None, None
    
    for part in parts:
        if not part:
            return None, None
        if not part.isdigit():
            return None, None
    
    return token, text


def _get_parent_id(node_id):
    """
    Возвращает parent id для данного id.
    Примеры:
      '1.2.1.' -> '1.2.'
      '1.2.' -> '1.'
      '1.' -> None
    """
    if not node_id or not node_id.endswith('.'):
        return None
    
    parts = node_id[:-1].split('.')
    if len(parts) <= 1:
        return None
    
    return '.'.join(parts[:-1]) + '.'


def _build_hierarchical_tree(flat_blocks):
    """
    Строит иерархическое дерево из плоского списка блоков.
    
    flat_blocks - список dict с ключами: id, start, end, text
    
    Возвращает список корневых узлов, где каждый узел имеет структуру:
    {
      "id": "1.",
      "start": 0.0,
      "end": 1.5,
      "text": "some text",
      "children": [...]
    }
    """
    if not flat_blocks:
        return []
    
    # Создаём словарь для быстрого доступа к узлам по id
    nodes = {}
    for block in flat_blocks:
        node_id = block['id']
        nodes[node_id] = {
            'id': node_id,
            'start': block['start'],
            'end': block['end'],
            'text': block['text'],
            'children': []
        }
    
    # Находим корневые узлы и строим дерево
    roots = []
    for node_id, node in nodes.items():
        parent_id = _get_parent_id(node_id)
        if parent_id is None:
            # Это корневой узел
            roots.append(node)
        elif parent_id in nodes:
            # Добавляем к родителю
            nodes[parent_id]['children'].append(node)
        # Если родитель не найден, узел игнорируется (не добавляется в дерево)
    
    return roots


def _build_markers_text(scene, fps):
    """
    Строит иерархический список markers_text из timeline markers и активного текстового блока.
    Возвращает список корневых узлов или пустой список, если нет достаточно данных.
    """
    try:
        # Получаем timeline markers
        markers = scene.timeline_markers
        if len(markers) < 2:
            return []
        
        # Сортируем маркеры по frame
        sorted_markers = sorted(markers, key=lambda m: m.frame)
        
        # Получаем текст из активного Text Editor
        text_datablock = get_active_text_datablock()
        if not text_datablock:
            return []
        
        text_content = text_datablock.as_string()
        if not text_content:
            return []
        
        # Парсим текст на блоки
        raw_blocks = _parse_text_blocks(text_content)
        if not raw_blocks:
            return []
        
        # Парсим каждый блок, извлекая id и text
        parsed_blocks = []
        for block in raw_blocks:
            node_id, text = _parse_id_and_text(block)
            if node_id is not None:
                parsed_blocks.append({'id': node_id, 'text': text})
        
        if not parsed_blocks:
            return []
        
        # Формируем marker ranges и мапим на блоки
        flat_blocks = []
        fps_real = fps  # Уже учитывает fps_base
        
        for i in range(len(sorted_markers) - 1):
            if i >= len(parsed_blocks):
                # Блоков меньше чем marker ranges - игнорируем остальные ranges
                break
            
            parsed_block = parsed_blocks[i]
            marker_start = sorted_markers[i]
            marker_end = sorted_markers[i + 1]
            
            # Вычисляем время в секундах (абсолютное, не относительно frame_start)
            start_seconds = float(marker_start.frame) / fps_real
            end_seconds = float(marker_end.frame) / fps_real
            
            # Округляем до 2 знаков
            start_seconds = round(start_seconds, 2)
            end_seconds = round(end_seconds, 2)
            
            flat_blocks.append({
                'id': parsed_block['id'],
                'start': start_seconds,
                'end': end_seconds,
                'text': parsed_block['text']
            })
        
        # Строим иерархическое дерево
        tree = _build_hierarchical_tree(flat_blocks)
        return tree
    
    except Exception:
        # Если что-то пошло не так - возвращаем пустой список (robustness)
        return []


# -------------------------
# Baking helpers (evaluated local transform)
# -------------------------

def _is_quaternion_constant(values, eps=1e-6):
    if not values:
        return True
    vs = [float(v) for v in values]
    if len(vs) < 8:
        return True
    base = vs[0:4]
    for i in range(4, len(vs), 4):
        if any(abs(vs[i + j] - base[j]) > eps for j in range(4)):
            return False
    return True


def _eval_local_matrix(obj, depsgraph):
    """
    Возвращает локальную матрицу относительно (evaluated) родителя.
    Нужна для корректного совпадения с glTF и учёта constraints/drivers.
    """
    obj_eval = obj.evaluated_get(depsgraph)
    mw = obj_eval.matrix_world.copy()

    if obj.parent:
        p_eval = obj.parent.evaluated_get(depsgraph)
        pmw = p_eval.matrix_world.copy()
        try:
            return pmw.inverted() @ mw
        except Exception:
            return mw

    return mw


# -------------------------
# Основная сборка клипа
# -------------------------

def build_three_clip_from_saved_entry(entry_name, entry):
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    depsgraph = bpy.context.evaluated_depsgraph_get()

    fps = _get_scene_fps(scene)
    export_alpha = bool(getattr(scene, "umz_export_alpha_tracks", True))

    try:
        frame_start = int(entry.get("frame_start", scene.frame_start))
    except Exception:
        frame_start = int(scene.frame_start)

    try:
        frame_end = int(entry.get("frame_end", scene.frame_end))
    except Exception:
        frame_end = int(scene.frame_end)

    duration = float(frame_end - frame_start) / float(fps)

    tracks_out = []
    alpha_tracks_out = []
    
    # --- режим видимости для three.js (кладём node_id, а не Blender name) ---
    visible_nodes_mode = "ALL"
    visible_nodes = None

    mode = entry.get("visible_objects_mode", "ALL")
    if mode == "SELECTED":
        names = entry.get("visible_objects") or []
        if isinstance(names, list) and names:
            visible_nodes_mode = "SELECTED"
            scene_objects = {o.name: o for o in bpy.data.objects}
            tmp = []
            for n in names:
                obj = scene_objects.get(n)
                if obj:
                    tmp.append(_safe_node_id(obj))
            visible_nodes = tmp    

    scene_objects = {o.name: o for o in bpy.data.objects}

    for tr in entry.get("tracks", []):
        obj_name_blender = tr.get("object_name")
        anim = tr.get("animation") or {}
        if not obj_name_blender or not isinstance(anim, dict):
            continue

        obj = scene_objects.get(obj_name_blender)
        if not obj:
            continue

        node_id = _safe_node_id(obj)
        is_cam = _is_camera_object(obj)

        # --- собираем список всех fcurves (dict) чтобы быстро понять, что вообще анимируется ---
        fcurves_all = []
        for nla_track in (anim.get("tracks") or []):
            for strip in (nla_track.get("strips") or []):
                act = strip.get("action") or {}
                for fc in (act.get("fcurves") or []):
                    fcurves_all.append(fc)

        if not fcurves_all:
            continue

        def _has_dp(dp):
            for fc in fcurves_all:
                if fc.get("data_path") == dp:
                    return True
            return False

        has_loc = _has_dp("location") or _has_dp("delta_location")
        has_rot = _has_dp("rotation_quaternion") or _has_dp("rotation_euler")
        has_alpha = _has_dp("color") or _has_dp('["alpha"]')

        # -------------------------
        # alpha_tracks (отдельно, линейно)
        # -------------------------
        if export_alpha and has_alpha:
            at = _build_alpha_tracks_for_object(node_id, anim, frame_start, frame_end, fps)
            if at:
                alpha_tracks_out.append(at)

        fade = _collect_nla_keyframes(anim, '["fade"]', 0, frame_start, frame_end)

        # -------------------------
        # Position: keys-only (но значения берём из depsgraph)
        # Камеру можно печь чаще/регулярно
        # -------------------------
        if has_loc:
            pos_frames = set()
            pos_frames.update(_collect_nla_keyframes_frames(anim, "location", 0, frame_start, frame_end))
            pos_frames.update(_collect_nla_keyframes_frames(anim, "location", 1, frame_start, frame_end))
            pos_frames.update(_collect_nla_keyframes_frames(anim, "location", 2, frame_start, frame_end))

            if not pos_frames:
                pos_frames.update(_collect_nla_keyframes_frames(anim, "delta_location", 0, frame_start, frame_end))
                pos_frames.update(_collect_nla_keyframes_frames(anim, "delta_location", 1, frame_start, frame_end))
                pos_frames.update(_collect_nla_keyframes_frames(anim, "delta_location", 2, frame_start, frame_end))

            if CAMERA_BAKE_EVERY_FRAME and is_cam:
                frames = list(range(frame_start, frame_end + 1, int(CAMERA_BAKE_STEP_FRAMES)))
            else:
                frames = sorted(pos_frames)

            if frames:
                times = [_frame_to_time(fr, frame_start, fps) for fr in frames]
                values = []

                current_frame = scene.frame_current
                for fr in frames:
                    try:
                        scene.frame_set(int(fr))
                        view_layer.update()
                    except Exception:
                        pass

                    ml = _eval_local_matrix(obj, depsgraph)
                    loc, rot, sca = ml.decompose()
                    values.extend([float(loc.x), float(loc.y), float(loc.z)])

                try:
                    scene.frame_set(current_frame)
                    view_layer.update()
                except Exception:
                    pass

                tracks_out.append({
                    "type": "vector",
                    "name": f"{node_id}.position",
                    "times": times,
                    "values": values
                })

        # -------------------------
        # Rotation: quaternion bake (шаг фиксированный), для камеры можно чаще
        # -------------------------
        if has_rot:
            if CAMERA_BAKE_EVERY_FRAME and is_cam:
                frames = list(range(frame_start, frame_end + 1, int(CAMERA_BAKE_STEP_FRAMES)))
            else:
                frames = list(range(frame_start, frame_end + 1, int(ROT_BAKE_STEP_FRAMES)))

            if not frames:
                frames = [frame_start, frame_end]
            if frames[-1] != frame_end:
                frames.append(frame_end)

            times = [_frame_to_time(fr, frame_start, fps) for fr in frames]

            quat_values = []
            prev_q = None
            current_frame = scene.frame_current

            for fr in frames:
                try:
                    scene.frame_set(int(fr))
                    view_layer.update()
                except Exception:
                    pass

                ml = _eval_local_matrix(obj, depsgraph)
                loc, rot, sca = ml.decompose()

                q = rot.to_quaternion() if hasattr(rot, "to_quaternion") else rot
                q.normalize()

                # фикс "переворота" кватерниона: чтобы не было скачков из-за смены знака
                if prev_q is not None and prev_q.dot(q) < 0.0:
                    q = Quaternion((-q.w, -q.x, -q.y, -q.z))
                prev_q = q.copy()

                quat_values.extend([float(q.x), float(q.y), float(q.z), float(q.w)])

            try:
                scene.frame_set(current_frame)
                view_layer.update()
            except Exception:
                pass

            # если кватернион константный — можно не писать трек
            if not _is_quaternion_constant(quat_values):
                tracks_out.append({
                    "type": "quaternion",
                    "name": f"{node_id}.quaternion",
                    "times": times,
                    "values": quat_values
                })

        # -------------------------
        # Fade -> userData.fade (обычный number track)
        # -------------------------
        if fade:
            frames = _union_frames(fade)
            if frames:
                def value_at_step(channel_pts, frame):
                    if not channel_pts:
                        return 0.0
                    last = channel_pts[0][1]
                    for fr, val in channel_pts:
                        if fr == frame:
                            return val
                        if fr < frame:
                            last = val
                        if fr > frame:
                            break
                    return last

                tracks_out.append(_build_number_track(
                    node_id,
                    "userData.fade",
                    frames,
                    frame_start,
                    fps,
                    lambda fr: value_at_step(fade, fr)
                ))

    out = {
        "name": entry_name,
        "fps": fps,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "duration": duration,
        "tracks": tracks_out,
        "alpha_tracks": alpha_tracks_out,
        "visible_nodes_mode": visible_nodes_mode,
    }

    if visible_nodes_mode == "SELECTED":
        out["visible_nodes"] = visible_nodes or []

    # Добавляем markers_text если есть маркеры и текст
    markers_text = _build_markers_text(scene, fps)
    if markers_text:
        out["markers_text"] = markers_text

    return out


def write_three_animation_to_file(name, clip, folder):
    """
    Записывает three_<name>.json в папку folder.
    Папку мы передаём снаружи (обычно из storage.get_external_folder()).
    """
    if not folder:
        return False
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"three_{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(clip, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False