# event_logger.py

class EventLogger:

    def __init__(self, db):
        self.db = db

    def log(
        self,
        level,
        event_type,
        message
    ):

        self.db.insert_system_event(
            level,
            event_type,
            message
        )