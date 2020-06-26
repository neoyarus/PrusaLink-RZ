import logging
import re
from threading import Thread
from typing import Union, Dict

from blinker import Signal

from old_buddy.modules.connect_api import Telemetry, Sources, States
from old_buddy.modules.inserters import telemetry_inserters
from old_buddy.modules.serial import Serial
from old_buddy.settings import QUIT_INTERVAL, STATUS_UPDATE_INTERVAL_SEC, STATE_MANAGER_LOG_LEVEL
from old_buddy.util import run_slowly_die_fast, get_command_id

log = logging.getLogger(__name__)
log.setLevel(STATE_MANAGER_LOG_LEVEL)

OK_REGEX = re.compile(r"^ok$")
BUSY_REGEX = re.compile("^echo:busy: processing$")
ATTENTION_REGEX = re.compile("^echo:busy: paused for user$")
PAUSED_REGEX = re.compile("^// action:paused$")
RESUMED_REGEX = re.compile("^// action:resumed$")
CANCEL_REGEX = re.compile("^// action:cancel$")
START_PRINT_REGEX = re.compile(r"^echo:enqueing \"M24\"$")
PRINT_DONE_REGEX = re.compile(r"^Done printing file$")
ERROR_REGEX = re.compile(r"^Error:Printer stopped due to errors. Fix the error and use M999 to restart.*")

SD_PRINTING_REGEX = re.compile(r"^(Not SD printing)$|^(\d+:\d+)$")

PRINTING_STATES = {States.PRINTING, States.PAUSED, States.FINISHED}


class StateChange:

    def __init__(self, api_response=None, to_states: Dict[States, Union[Sources, None]] = None,
                 from_states: Dict[States, Union[Sources, None]] = None, default_source: Sources = None):

        self.to_states: Dict[States, Union[Sources, None]] = {}
        self.from_states: Dict[States, Union[Sources, None]] = {}

        if from_states is not None:
            self.from_states = from_states
        if to_states is not None:
            self.to_states = to_states

        self.api_response = api_response
        self.default_source = default_source


def state_influencer(state_change: StateChange = None):

    """
    This decorator makes it possible for each state change to have default expected sources
    This can be overridden by notifying the state manager about an oncoming state change through expect_change
    """
    def inner(func):
        def wrapper(self, *args, **kwargs):

            has_set_expected_change = False
            if self.expected_state_change is None and state_change is not None:
                has_set_expected_change = True
                self.expect_change(state_change)

            else:
                log.debug(f"Default expected state change is overriden")

            func(self, *args, **kwargs)
            self.state_may_have_changed()

            if has_set_expected_change:
                self.stop_expecting_change()

        return wrapper
    return inner


class StateManager:
    state_changed = Signal()  # kwargs: command_id: int, source: Sources

    instance = None  # Just checks if there is not more than one instance in existence, not a singleton!

    def __init__(self, serial: Serial):
        if self.instance is not None:
            raise AssertionError("If this is required, we need the signals moved from class to instance variables.")

        self.running = True

        # The ACTUAL states considered when reporting
        self.base_state: States = States.READY
        self.printing_state: Union[None, States] = None
        self.override_state: Union[None, States] = None

        # Flag that can be flipped by base state, or internally, so nothing would be sent to the printer.
        self.internal_busy = False

        # Reported state history
        self.last_state = self.get_state()
        self.current_state = self.get_state()

        # Another anti-ideal thing is, that with this observational approach to state detection
        # we cannot correlate actions with reactions nicely. My first approach is to have an action,
        # that's supposed to change the state and to which statethat shall be
        # if we observe such a transition, we'll say the action caused the state change
        self.expected_state_change: Union[None, StateChange] = None

        self.serial: Serial = serial

        self.serial.register_output_handler(OK_REGEX, lambda match: self.ok())
        self.serial.register_output_handler(BUSY_REGEX, lambda match: self.busy())
        self.serial.register_output_handler(ATTENTION_REGEX, lambda match: self.attention())
        self.serial.register_output_handler(PAUSED_REGEX, lambda match: self.paused())
        self.serial.register_output_handler(RESUMED_REGEX, lambda match: self.resumed())
        self.serial.register_output_handler(CANCEL_REGEX, lambda match: self.not_printing())
        self.serial.register_output_handler(START_PRINT_REGEX, lambda match: self.printing())
        self.serial.register_output_handler(PRINT_DONE_REGEX, lambda match: self.finished())
        self.serial.register_output_handler(ERROR_REGEX, lambda match: self.error())

        self.state_thread = Thread(target=self._keep_updating_state, name="State updater")
        self.state_thread.start()

    def _keep_updating_state(self):
        run_slowly_die_fast(lambda: self.running, QUIT_INTERVAL, STATUS_UPDATE_INTERVAL_SEC, self.update_state)

    def stop(self):
        self.running = False
        self.state_thread.join()

    def update_state(self):
        if self.is_busy():
            log.debug("Not bothering with state updates, sending PRUSA PING, for us to know, "
                      "when the printer starts responding again")
            self.serial.write("PRUSA PING")
            return

        if self.printing_state == States.PRINTING:
            # Using telemetry inserting function as a getter, if this would be done multiple times please separate
            # "inserters" from getters
            try:
                progress = telemetry_inserters.insert_progress(self.serial, Telemetry()).progress
            except TimeoutError:
                log.debug("Printer did not tell us the progress percentage in time")
            else:
                if progress == 100:
                    self.expect_change(StateChange(to_states={States.FINISHED: Sources.MARLIN}))
                    self.finished()

        try:
            match = self.serial.write("M27", SD_PRINTING_REGEX)
        except TimeoutError:
            log.debug("Printer did not report if it's printing or not :(")
        else:
            groups = match.groups()
            # FIXME: Do not go out of the printing state when paused, cannot be detected/maintained otherwise
            if groups[0] is not None and self.printing_state != States.PAUSED:  # Not printing
                self.not_printing()
            else:  # Printing
                self.printing()

    def get_state(self):
        if self.override_state is not None:
            return self.override_state
        elif self.printing_state is not None:
            return self.printing_state
        else:
            return self.base_state

    def is_busy(self):
        """
        There is two busy states, one is the printer observation one, which is stored in the base_state
        the other one is internal_busy, which can be changed by Old Buddy components for stuff like writing a file
        where every telemetry request would get mistakenly written to the file being created.
        """
        return self.internal_busy or self.base_state == States.BUSY

    def expect_change(self, change: StateChange):
        self.expected_state_change = change

    def stop_expecting_change(self):
        self.expected_state_change = None

    def is_expected(self):
        state_change = self.expected_state_change
        expecting_change = state_change is not None
        if expecting_change:
            expected_to = self.current_state in state_change.to_states
            expected_from = self.last_state in state_change.from_states
            has_default_source = state_change.default_source is not None
            return expected_to or expected_from or has_default_source
        else:
            return False

    def expected_source(self):
        # No change expected,
        if self.expected_state_change is None:
            return None

        state_change = self.expected_state_change

        # Get the expected sources
        source_from = None
        source_to = None
        if self.last_state in state_change.from_states:
            source_from = state_change.from_states[self.last_state]
        if self.current_state in state_change.to_states:
            source_to = state_change.to_states[self.current_state]

        # If there are conflicting sources, pick the one, paired with from_state as this is useful for leaving
        # states like ATTENTION and ERROR
        if source_from is not None and source_to is not None and source_to != source_from:
            source = source_from
        else:  # no conflict here, the sources are the same, or one or both of them are None
            try:  # make a list throwing out Nones and get the next item (the first on)
                source = next(item for item in [source_from, source_to] if item is not None)
            except StopIteration:  # tried to get next from an empty list
                source = None

        if source is None:
            source = state_change.default_source

        log.debug(f"Source has been determined to be {source}. "
                  f"Default was: {state_change.default_source}, from: {source_from}, to: {source_to}")

        return source

    def state_may_have_changed(self):
        # Did our internal state change cause a change compared to our state history? If yes, update state stuff
        if self.get_state() != self.current_state:
            self.last_state = self.current_state
            self.current_state = self.get_state()
            log.debug(f"Changing state from {self.last_state} to {self.current_state}")

            # Now let's find out if the state change was expected and what parameters can we deduce from that
            command_id = None
            source = None

            if self.printing_state is not None:
                log.debug(f"We are printing - {self.printing_state}")

            if self.override_state is not None:
                log.debug(f"State is overridden by {self.override_state}")

            # If the state changed to something expected, then send the information about it
            if self.is_expected():
                if self.expected_state_change.api_response is not None:
                    command_id = get_command_id(self.expected_state_change.api_response)
                source = self.expected_source().name
            else:
                log.debug("Unexpected state change. This is weird")
            self.expected_state_change = None
            self.state_changed.send(self, command_id=command_id, source=source)

    # --- State changing methods ---

    # This state change can change the state to "PRINTING"
    @state_influencer(StateChange(to_states={States.PRINTING: Sources.USER}))
    def printing(self):
        if self.printing_state is None:
            self.printing_state = States.PRINTING

    @state_influencer(StateChange(from_states={States.PRINTING: Sources.MARLIN,
                                               States.PAUSED: Sources.MARLIN,
                                               States.FINISHED: Sources.MARLIN}))
    def not_printing(self):
        if self.printing_state is not None:
            self.printing_state = None

    @state_influencer(StateChange(to_states={States.FINISHED: Sources.MARLIN}))
    def finished(self):
        if self.printing_state == States.PRINTING:
            self.printing_state = States.FINISHED

    @state_influencer(StateChange(to_states={States.BUSY: Sources.MARLIN}))
    def busy(self):
        if self.base_state == States.READY:
            self.base_state = States.BUSY

    # Cannot distinguish pauses from the uuser and the gcode
    @state_influencer(StateChange(to_states={States.PAUSED: Sources.USER}))
    def paused(self):
        if self.printing_state == States.PRINTING:
            self.printing_state = States.PAUSED

    @state_influencer(StateChange(to_states={States.PRINTING: Sources.USER}))
    def resumed(self):
        if self.printing_state == States.PAUSED:
            self.printing_state = States.PRINTING

    @state_influencer(
        StateChange(to_states={States.READY: Sources.MARLIN}, from_states={States.ATTENTION: Sources.USER,
                                                                           States.ERROR: Sources.USER}))
    def ok(self):
        if self.override_state is not None:
            log.debug(f"No longer having state {self.override_state}")
            self.override_state = None
            self.state_may_have_changed()

        if self.printing_state == States.FINISHED:
            self.printing_state = None

        if self.base_state == States.BUSY:
            self.base_state = States.READY

    @state_influencer(StateChange(to_states={States.ATTENTION: Sources.USER}))
    def attention(self):
        log.debug(f"Overriding the state with ATTENTION")
        self.override_state = States.ATTENTION

    @state_influencer(StateChange(to_states={States.ERROR: Sources.WUI}))
    def error(self):
        log.debug(f"Overriding the state with ERROR")
        self.override_state = States.ERROR