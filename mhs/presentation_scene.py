"""Project-only ceiling fill lights for the Smart Hospital presentation.

No devices, camera products, sensor readings, physics, navigation or clinical
lighting commands are created or changed. All authored prims live in one
anonymous session sublayer under /World/HospitalPresentation. Config polling
accepts a small fixed schema, never code, arbitrary prim paths or positions.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid


CONFIG_KEYS = frozenset(("schema_version", "operation_id", "enabled", "intensity", "exposure"))
MAX_LIGHTS = 18
LIGHT_WIDTH = LIGHT_HEIGHT = 1.0
BASE_PATH = "/World/HospitalPresentation"
POLL_SECONDS = 1.0


def validate_config(value):
    if not isinstance(value, dict) or set(value) != CONFIG_KEYS:
        raise ValueError("presentation_config_fields_invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("presentation_config_version_invalid")
    if not isinstance(value["operation_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", value["operation_id"]):
        raise ValueError("presentation_operation_id_invalid")
    if type(value["enabled"]) is not bool:
        raise ValueError("presentation_enabled_must_be_boolean")
    for name, low, high in (("intensity", 0.0, 30000.0), ("exposure", -1.0, 1.0)):
        number = value[name]
        if (isinstance(number, bool) or not isinstance(number, (int, float))
                or not math.isfinite(number) or not low <= number <= high):
            raise ValueError("presentation_" + name + "_out_of_bounds")
    return dict(value)


def _atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _bounds_valid(bounds):
    return (isinstance(bounds, list) and len(bounds) == 6
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in bounds)
            and all(bounds[index] <= bounds[index + 3] for index in range(3)))


def _contains_xy(bounds, x, y, margin=0.0):
    return bounds[0] + margin <= x <= bounds[3] - margin and bounds[1] + margin <= y <= bounds[4] - margin


def _overlaps(first, second):
    return all(first[index] <= second[index + 3] and first[index + 3] >= second[index] for index in range(3))


class PresentationScene:
    """Construct once after SmartWardScene, then call poll(sim_time) each loop.

    Placement reuses this scene's original-geometry audit, verifies selected
    roof/furniture prim bounds from the live USD stage, and refuses unsupported
    candidates. Presentation settings remain separate from two business lamps.
    """

    def __init__(self, root, stage):
        from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics

        self.root, self.stage = Path(root), stage
        self.Gf, self.Sdf, self.Usd, self.UsdGeom, self.UsdLux = Gf, Sdf, Usd, UsdGeom, UsdLux
        self.CollisionAPI = UsdPhysics.CollisionAPI
        self.directory = self.root / "runs/smart-ward-v1/presentation"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "configs/presentation-scene.json"
        self.instance_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:12]
        self.last_poll = float("-inf")
        self.last_input_hash = None
        self.last_error = None
        self.applied_operations = {}
        self.config = None
        self.lights = {}
        self.placements, self.rejections = [], []
        self.layer = Sdf.Layer.CreateAnonymous("hospital-presentation-" + self.instance_id + ".usda")
        stage.GetSessionLayer().subLayerPaths.insert(0, self.layer.identifier)
        self.bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
        self._actual_bounds_cache = {}
        self.placement_sources = {}
        try:
            self._prepare_placements()
        except Exception as error:
            # A presentation-only failure must not replace business/physics
            # evidence or prevent an otherwise healthy hospital from starting.
            self.rejections.append({"id": "all", "status": "NA", "reason": repr(error)})
        with Usd.EditContext(stage, self.layer):
            UsdGeom.Xform.Define(stage, BASE_PATH)
            for placement in self.placements:
                light = UsdLux.RectLight.Define(stage, placement["prim_path"])
                light.CreateWidthAttr(LIGHT_WIDTH)
                light.CreateHeightAttr(LIGHT_HEIGHT)
                api = UsdLux.LightAPI(light.GetPrim())
                api.CreateColorAttr(Gf.Vec3f(1.0, 1.0, 1.0))
                api.CreateNormalizeAttr(True)
                api.CreateIntensityAttr(0.0)
                api.CreateExposureAttr(0.0)
                # In this Z-up scene the unrotated RectLight emits toward -Z.
                UsdGeom.Xformable(light.GetPrim()).AddTranslateOp().Set(Gf.Vec3d(*placement["position"]))
                self.lights[placement["id"]] = light
        self.poll(0.0, force=True)

    def _actual_bounds(self, row):
        path = row["path"]
        if path in self._actual_bounds_cache:
            return self._actual_bounds_cache[path]
        prim = self.stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid() or not self.UsdGeom.Boundable(prim):
            raise ValueError("presentation_geometry_prim_unavailable:" + path)
        box = self.bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if box.IsEmpty():
            raise ValueError("presentation_geometry_bound_empty:" + path)
        bounds = [*map(float, box.GetMin()), *map(float, box.GetMax())]
        if not _bounds_valid(bounds):
            raise ValueError("presentation_geometry_bound_invalid:" + path)
        self._actual_bounds_cache[path] = bounds
        return bounds

    def _prepare_placements(self):
        layout_path = self.root / "configs/hospital-v2.json"
        audit_path = self.root / "runs/smart-ward-v1/scene/placement-audit.json"
        layout_bytes, audit_bytes = layout_path.read_bytes(), audit_path.read_bytes()
        layout, audit = json.loads(layout_bytes), json.loads(audit_bytes)
        if audit.get("source") != "USD_instance_proxy_geometry_and_collision_inventory":
            raise ValueError("presentation_current_geometry_audit_required")
        rows = [row for row in audit.get("inventory", [])
                if isinstance(row, dict) and isinstance(row.get("path"), str)
                and row["path"].startswith("/World/hospital/") and _bounds_valid(row.get("bounds"))
                and row.get("decision") != "exclude"]
        roofs = [row for row in rows if any(token in row["path"].lower() for token in ("roof", "ceiling"))
                 and 2.5 <= row["bounds"][2] <= 4.0]
        if not roofs:
            raise ValueError("presentation_original_roof_geometry_required")
        rooms = {room["id"]: room for room in layout["rooms"]}
        views = layout["inspection_views"]
        self.protected_rooms = {name: rooms[name]["bounds"] for name in ("ward301", "ward303")}
        center = lambda name: [(rooms[name]["bounds"][0] + rooms[name]["bounds"][2]) / 2,
                               (rooms[name]["bounds"][1] + rooms[name]["bounds"][3]) / 2]
        reception = center("reception")
        west_eye, east_eye = views["west_wards"]["eye"], views["east_wards"]["eye"]
        candidates = [
            ("reception", reception, "audited reception room center"),
            ("entry", views["reception"]["eye"][:2], "actual reception inspection camera location"),
            ("pharmacy_approach", views["pharmacy"]["eye"][:2], "actual pharmacy inspection camera location"),
            ("east_lobby_turn", [7.625, 1.575], "previous real Nav2-verified Lab route point"),
            ("east_main", [11.125, 5.075], "previous real Nav2-verified Lab route point"),
            ("lab_approach", [17.625, 2.575], "previous real Nav2-verified Lab route point"),
            ("central", [-12.0, 10.65], "corridor between supply and central nurse approach"),
            ("west_main", [(west_eye[0] - 12.0) / 2, 11.8], "main west corridor before ward entrances"),
            ("west_entry", [west_eye[0], 12.0], "west inspection corridor below ward301 boundary"),
            ("east_entry", [east_eye[0], 11.5], "east inspection corridor below ward303 boundary"),
            ("west_ward_entrance", [-40.0, 17.0], "audited west corridor, outside ward301"),
            ("west_ward_middle", [-40.0, 24.0], "audited west ward corridor"),
            ("west_ward_far", [-40.0, 30.0], "audited west ward corridor near human patrol"),
            ("east_ward_entrance", [19.0, 16.0], "audited east corridor, outside ward303"),
            ("east_ward_middle", [19.0, 23.0], "actual east inspection corridor"),
            ("east_ward_far", [19.0, 28.0], "corridor outside C nurse station"),
            ("nurse_b_counter", center("nurse_b"), "actual west nurse room, outside business lighting wards"),
            ("nurse_c_counter", center("nurse_c"), "actual east nurse room, outside business lighting wards"),
        ]
        self.placement_sources = {"layout_sha256": hashlib.sha256(layout_bytes).hexdigest(),
                                 "geometry_audit_sha256": hashlib.sha256(audit_bytes).hexdigest(),
                                 "geometry_audit_path": str(audit_path),
                                 "live_USD_bbox_verification": True,
                                 "stage_up_axis": str(self.UsdGeom.GetStageUpAxis(self.stage)),
                                 "stage_meters_per_unit": self.UsdGeom.GetStageMetersPerUnit(self.stage),
                                 "height_rule": "min(2.55m, live roof lower bound minus 0.30m); reject below 2.2m"}
        if self.UsdGeom.GetStageUpAxis(self.stage) != self.UsdGeom.Tokens.z:
            raise ValueError("presentation_requires_existing_Z_up_hospital")
        for ident, desired, basis in candidates[:MAX_LIGHTS]:
            selected = None
            failures = []
            # Search only the immediate audited area, never across rooms.
            offsets = sorted(((dx, dy) for dx in (0.0, -.4, .4, -.8, .8)
                              for dy in (0.0, -.4, .4, -.8, .8)), key=lambda pair: (math.hypot(*pair), pair))
            for dx, dy in offsets:
                x, y = desired[0] + dx, desired[1] + dy
                try:
                    if any(bounds[0] - .8 <= x <= bounds[2] + .8 and bounds[1] - .8 <= y <= bounds[3] + .8
                           for bounds in self.protected_rooms.values()):
                        raise ValueError("protected_business_lighting_room")
                    enclosing = [row for row in roofs if _contains_xy(row["bounds"], x, y, .55)]
                    actual_roofs = [(self._actual_bounds(row), row["path"]) for row in enclosing]
                    actual_roofs = [(bounds, path) for bounds, path in actual_roofs if _contains_xy(bounds, x, y, .55)]
                    if not actual_roofs:
                        raise ValueError("no_live_verified_roof_above_light")
                    roof, roof_path = min(actual_roofs, key=lambda item: item[0][2])
                    z = min(2.55, roof[2] - .30)
                    if z < 2.2:
                        raise ValueError("insufficient_ceiling_clearance")
                    footprint = [x - .52, y - .52, z - .05, x + .52, y + .52, z + .05]
                    possible = [row for row in rows if _overlaps(row["bounds"], footprint)]
                    blocking = [row["path"] for row in possible if _overlaps(self._actual_bounds(row), footprint)]
                    if blocking:
                        raise ValueError("live_geometry_intersects_light:" + blocking[0])
                    selected = {"id": ident, "prim_path": BASE_PATH + "/Fill_" + ident,
                                "requested_xy": list(desired), "position": [x, y, z], "placement_basis": basis,
                                "roof_prim": roof_path, "roof_actual_bounds": roof,
                                "checked_nearby_geometry_count": len(possible), "collision_authored": False,
                                "kind": "presentation_fill_not_a_business_device"}
                    break
                except ValueError as error:
                    failures.append(str(error))
            if selected is None:
                self.rejections.append({"id": ident, "requested_xy": list(desired), "status": "NA",
                                        "reason": failures[-1] if failures else "placement_unavailable"})
            else:
                self.placements.append(selected)

    def _readback(self):
        rows = []
        for ident, light in self.lights.items():
            api = self.UsdLux.LightAPI(light.GetPrim())
            rows.append({"id": ident, "prim_path": str(light.GetPath()),
                         "actual_intensity": float(api.GetIntensityAttr().Get()),
                         "actual_exposure": float(api.GetExposureAttr().Get()),
                         "actual_normalize": bool(api.GetNormalizeAttr().Get()),
                         "actual_color": list(api.GetColorAttr().Get()),
                         "width": float(light.GetWidthAttr().Get()), "height": float(light.GetHeightAttr().Get()),
                         "collision_api_present": bool(light.GetPrim().HasAPI(self.CollisionAPI))})
        return rows

    def poll(self, sim_time=None, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_poll < POLL_SECONDS:
            return None
        self.last_poll = now
        try:
            if self.config_path.is_symlink() or self.config_path.resolve().parent != (self.root / "configs").resolve():
                raise ValueError("presentation_config_path_invalid")
            raw = self.config_path.read_bytes()
            if len(raw) > 4096:
                raise ValueError("presentation_config_too_large")
            input_hash = hashlib.sha256(raw).hexdigest()
            if input_hash == self.last_input_hash:
                return None
            config = validate_config(json.loads(raw))
            semantic = json.dumps({key: config[key] for key in config if key != "operation_id"}, sort_keys=True)
            previous = self.applied_operations.get(config["operation_id"])
            if previous is not None and previous != semantic:
                raise ValueError("presentation_operation_id_payload_conflict")
            if previous is None and len(self.applied_operations) >= 128:
                raise ValueError("presentation_operation_limit_requires_new_instance")
            requested = float(config["intensity"]) if config["enabled"] else 0.0
            # Sunlit foyer needs less fill than the fully enclosed ward corridors.
            daylight_ids = {'entry', 'reception', 'pharmacy_approach', 'east_lobby_turn'}
            intensities = {ident: requested*(.18 if ident in daylight_ids else 1.) for ident in self.lights}
            with self.Usd.EditContext(self.stage, self.layer):
                for ident,light in self.lights.items():
                    api = self.UsdLux.LightAPI(light.GetPrim())
                    api.GetIntensityAttr().Set(intensities[ident])
                    api.GetExposureAttr().Set(float(config["exposure"]))
            observed = self._readback()
            if any(abs(row["actual_intensity"] - intensities[row['id']]) > 1e-5
                   or abs(row["actual_exposure"] - float(config["exposure"])) > 1e-5
                   or not row["actual_normalize"] for row in observed):
                raise RuntimeError("presentation_light_readback_mismatch")
            self.config, self.last_input_hash = config, input_hash
            self.applied_operations[config["operation_id"]] = semantic
            self.last_error = None
            proof = {"schema_version": 1, "instance_id": self.instance_id, "wall_time": time.time(),
                     "sim_time_observed": sim_time, "status": "applied" if self.lights else "NA",
                     "requested": config, "config_sha256": input_hash,
                     "intensity_by_light": intensities,
                     "anonymous_layer_identifier": self.layer.identifier,
                     "overlay_sha256": hashlib.sha256(self.layer.ExportToString().encode()).hexdigest(),
                     "attachment": "anonymous_session_sublayer", "authored_root": BASE_PATH,
                     "light_count": len(self.lights), "maximum_lights": MAX_LIGHTS,
                     "placements": self.placements, "placement_rejections": self.rejections,
                     "placement_sources": self.placement_sources, "lights_readback": observed,
                     "business_devices_changed": False, "business_light_properties_changed": False,
                     "sensor_formula_or_readings_changed": False, "physics_or_map_changed": False,
                     "RGB_render_products_created": 0,
                     "scope": "Presentation fill; scene appearance changes but no clinical illumination claim or business recovery evidence."}
            snapshot = self.directory / (self.instance_id + "-" + config["operation_id"] + "-" + str(time.time_ns()) + ".json")
            with snapshot.open("x", encoding="utf-8") as output:
                json.dump(proof, output, ensure_ascii=False, indent=2, allow_nan=False)
            _atomic_json(self.directory / "status.json", proof)
            return proof
        except Exception as error:
            fingerprint = (type(error).__name__, str(error))
            if fingerprint != self.last_error:
                self.last_error = fingerprint
                failure = {"schema_version": 1, "instance_id": self.instance_id, "status": "rejected",
                           "wall_time": time.time(), "sim_time_observed": sim_time,
                           "error_type": type(error).__name__, "error": str(error),
                           "last_verified_configuration": self.config,
                           "actual_lights_readback": self._readback(),
                           "scope": "Presentation failure only; no business or sensor fallback generated."}
                with (self.directory / (self.instance_id + "-rejected-" + str(time.time_ns()) + ".json")).open("x", encoding="utf-8") as output:
                    json.dump(failure, output, ensure_ascii=False, indent=2, allow_nan=False)
                _atomic_json(self.directory / "status.json", failure)
                print("PRESENTATION_SCENE_REJECTED", type(error).__name__, str(error), flush=True)
            return None
