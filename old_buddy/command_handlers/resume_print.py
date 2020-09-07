import logging
from old_buddy.command_handlers.try_until_state import TryUntilState
from old_buddy.default_settings import get_settings
from old_buddy.structures.model_classes import States

LOG = get_settings().LOG


log = logging.getLogger(__name__)
log.setLevel(LOG.COMMANDS_LOG_LEVEL)


class ResumePrint(TryUntilState):
    command_name = "resume print"

    def _run_command(self):
        self._try_until_state(gcode="M602", desired_state=States.PRINTING)

        if self.file_printer.printing:
            self.file_printer.resume()