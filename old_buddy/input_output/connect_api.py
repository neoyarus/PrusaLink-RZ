import logging
from time import time

from blinker import Signal
from pydantic import BaseModel
from requests import Session, RequestException

from old_buddy.settings import CONNECT_API_LOG_LEVEL
from old_buddy.structures.model_classes import EmitEvents, FileTree, Event

log = logging.getLogger(__name__)
log.setLevel(CONNECT_API_LOG_LEVEL)


class ConnectAPI:

    connection_error = Signal()  # kwargs: path: str, json_dict: Dict[str, Any]

    # Just checks if there is not more than one instance in existence,
    # but this is not a singleton!
    instance = None

    def __init__(self, address, port, token, tls=False):
        assert self.instance is None, "If running more than one instance" \
                                      "is required, consider moving the " \
                                      "signals from class to instance " \
                                      "variables."

        self.address = address
        self.port = port

        self.started_on = time()

        protocol = "https" if tls else "http"

        self.base_url = f"{protocol}://{address}:{port}"
        log.info(f"Prusa Connect is expected on address: {address}:{port}.")
        self.session = Session()
        self.session.headers['Printer-Token'] = token

    def send_dict(self, path: str, json_dict: dict):
        log.info(f"Sending to connect {path}")
        log.debug(f"request data: {json_dict}")
        timestamp_header = {"Timestamp": str(int(time()))}
        try:
            response = self.session.post(self.base_url + path, json=json_dict,
                                         headers=timestamp_header)
        except RequestException:
            self.connection_error.send(self, path=path, json_dict=json_dict)
            raise
        log.info(f"Got a response: {response.status_code}")
        log.debug(f"Response contents: {response.content}")
        return response

    def send_model(self, path: str, model: BaseModel):
        json_dict = model.dict(exclude_none=True)
        return self.send_dict(path, json_dict)

    def emit_event(self, emit_event: EmitEvents, command_id: int = None,
                   reason: str = None, state: str = None, source: str = None,
                   root: str = None, files: FileTree = None):
        """
        Logs errors, but stops their propagation, as this is called many many
        times and doing try/excepts everywhere would hinder readability
        """
        event = Event(event=emit_event.value, command_id=command_id,
                      reason=reason, state=state, source=source, root=root,
                      files=files)

        try:
            self.send_model("/p/events", event)
        except RequestException:
            # Errors get logged upstream, stop propagation,
            # try/excepting these would be a chore
            pass

    def stop(self):
        self.session.close()