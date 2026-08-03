from __future__ import annotations
import json
from pathlib import Path
from database_service import DatabaseService


def import_once(path: Path) -> None:
    db = DatabaseService()
    marker = db.conn.execute(
        "SELECT 1 FROM configuration_events WHERE event_type='legacy_robot_list_imported' LIMIT 1"
    ).fetchone()
    if marker:
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    for robot in data["robots"]:
        db.upsert_robot_type(robot["id"], robot.get("name", robot["id"]), robot.get("channels", 4))
    for blade in data["blade_types"]:
        # Existing JSON is inconsistent: V236 has six values but max_transitions=4.
        # Preserve all existing values while enforcing the new global maximum of eight.
        existing_max = max(
            [len(blades.get(blade["id"], [])) for blades in data["transitions"].values()] or [1]
        )
        max_t = min(8, max(int(blade.get("max_transitions", 8)), existing_max))
        db.upsert_blade_type(blade["id"], blade.get("name", blade["id"]), max_t)
    for robot_id, blades in data["transitions"].items():
        for blade_id, values in blades.items():
            db.upsert_default_transitions(
                robot_type_id=robot_id, blade_type_id=blade_id,
                values=[float(v) for v in values], source="legacy_robot.list.json")
    defaults = data.get("defaults", {})
    db.save_configuration_state(
        robot_type_id=defaults.get("robot_id"), blade_type_id=defaults.get("blade_id"),
        motor_direction=data.get("motor_direction", {}),
        motor_tuning=data.get("motor_tuning", {}),
    )
    with db._lock:
        db._insert_configuration_event_locked(
            "legacy_robot_list_imported", defaults.get("robot_id"), defaults.get("blade_id"),
            actor="migration", source=str(path), before=None, after={"version": data.get("version", 1)})
        db.conn.commit()


if __name__ == "__main__":
    import_once(Path(__file__).with_name("robot.list.json"))
