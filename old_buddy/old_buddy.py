import configparser
import logging
import threading
from distutils.util import strtobool
from json import JSONDecodeError
from time import time
from typing import Type

from requests import RequestException
from serial import SerialException

from old_buddy.command import Command
from old_buddy.command_handlers.execute_gcode import ExecuteGcode
from old_buddy.command_handlers.info_sender import RespondWithInfo
from old_buddy.command_handlers.pause_print import PausePrint
from old_buddy.command_handlers.reset_printer import ResetPrinter
from old_buddy.command_handlers.resume_print import ResumePrint
from old_buddy.command_handlers.start_print import StartPrint
from old_buddy.command_handlers.stop_print import StopPrint
from old_buddy.command_runner import CommandRunner
from old_buddy.file_printer import FilePrinter
from old_buddy.informers.filesystem.storage_controller import StorageController
from old_buddy.informers.job import Job
from old_buddy.informers.telemetry_gatherer import TelemetryGatherer
from old_buddy.informers.ip_updater import IPUpdater, NO_IP
from old_buddy.informers.state_manager import StateManager
from old_buddy.input_output.connect_api import ConnectAPI
from old_buddy.input_output.lcd_printer import LCDPrinter
from old_buddy.input_output.serial.serial import Serial
from old_buddy.input_output.serial.serial_queue \
    import MonitoredSerialQueue
from old_buddy.input_output.serial.helpers import enqueue_instruction
from old_buddy.input_output.serial.serial_reader import SerialReader
from old_buddy.model import Model
from old_buddy.default_settings import get_settings
from old_buddy.structures.model_classes import EmitEvents
from old_buddy.util import get_command_id, run_slowly_die_fast

LOG = get_settings().LOG
TIME = get_settings().TIME
SERIAL = get_settings().SERIAL
CONN = get_settings().CONN


log = logging.getLogger(__name__)
log.setLevel(LOG.OLD_BUDDY_LOG_LEVEL)


class OldBuddy:

    def __init__(self):
        self.running = True
        self.stopped_event = threading.Event()

        self.model = Model()
        self.serial_reader = SerialReader()

        try:
            self.serial = Serial(self.serial_reader,
                                 port=SERIAL.PRINTER_PORT,
                                 baudrate=SERIAL.PRINTER_BAUDRATE)
        except SerialException:
            log.exception(
                "Cannot talk to the printer using the RPi port, "
                "is it enabled? Is the Pi configured correctly?")
            raise

        self.serial_queue = MonitoredSerialQueue(self.serial,
                                                 self.serial_reader)

        self.config = configparser.ConfigParser()
        self.config.read(CONN.CONNECT_CONFIG_PATH)

        try:
            connect_config = self.config["connect"]
            address = connect_config["address"]
            port = connect_config["port"]
            token = connect_config["token"]
            try:
                tls = strtobool(connect_config["tls"])
            except KeyError:
                tls = False
        except KeyError:
            enqueue_instruction(self.serial_queue, "M117 Bad Old Buddy config")
            log.exception(
                "Config load failed, lan_settings.ini missing or invalid.")
            raise

        self.connect_api = ConnectAPI(address=address, port=port, token=token,
                                      tls=tls)
        ConnectAPI.connection_error.connect(self.connection_error)

        self.telemetry_gatherer = TelemetryGatherer(self.serial_reader,
                                                    self.serial_queue)
        self.telemetry_gatherer.updated_signal.connect(self.telemetry_gathered)
        # let's do this manually, for the telemetry to be known to the model
        # before connect can ask stuff
        self.telemetry_gatherer.update()

        self.file_printer = FilePrinter(self.serial_queue, self.serial_reader,
                                        self.telemetry_gatherer)

        self.state_manager = StateManager(self.serial_reader, self.file_printer)
        self.state_manager.state_changed_signal.connect(self.state_changed)
        self.state_manager.job_id_updated_signal.connect(self.job_id_updated)

        # Write the initial state to the model
        self.model.state = self.state_manager.get_state()

        # TODO: Hook onto the events
        self.job_id = Job()

        self.lcd_printer = LCDPrinter(self.serial_queue)

        self.storage = StorageController(self.serial_queue, self.serial_reader,
                                         self.state_manager)
        self.storage.updated_signal.connect(self.storage_updated)
        self.storage.inserted_signal.connect(self.media_inserted)
        self.storage.ejected_signal.connect(self.media_ejected)

        self.storage.sd_state_changed_signal.connect(self.sd_state_changed)
        # after connecting all the signals, do the first update manually
        self.storage.update()

        # Greet the user
        self.lcd_printer.enqueue_greet()

        # Start the local_ip updater after we enqueued the greetings
        self.ip_updater = IPUpdater()
        self.ip_updater.updated_signal.connect(self.ip_updated)

        # again, let's do the first one manually
        self.ip_updater.update()

        # Start individual informer threads after updating manually, so nothing
        # will race with itself
        self.telemetry_gatherer.start()
        self.storage.start()
        self.ip_updater.start()

        self.command_runner = CommandRunner(self.serial, self.serial_reader,
                                            self.serial_queue,
                                            self.connect_api,
                                            self.state_manager,
                                            self.file_printer, self.model)

        self.last_sent_telemetry = time()

        # After the initial states are distributed throughout the model,
        # let's open ourselves to some commands from connect
        self.connect_thread = threading.Thread(
            target=self.keep_sending_telemetry, name="connect_thread")
        self.connect_thread.start()

        # Start this last, as it might start printing right away
        self.file_printer.start()

    def stop(self):
        self.running = False
        self.connect_thread.join()
        self.storage.stop()
        self.lcd_printer.stop()
        self.command_runner.stop()
        self.telemetry_gatherer.stop()
        self.ip_updater.stop()
        self.serial_queue.stop()
        self.serial.stop()
        self.connect_api.stop()

        log.debug("Remaining threads, that could prevent us from quitting:")
        for thread in threading.enumerate():
            log.debug(thread)
        self.stopped_event.set()

    # --- API response handlers ---

    def handle_telemetry_response(self, api_response):
        if api_response.status_code == 200:
            log.debug(f"Command id -> {get_command_id(api_response)}")
            if api_response.headers["Content-Type"] == "text/x.gcode":
                self.command_runner.run(ExecuteGcode, api_response)
            else:
                self.determine_command(api_response)
        elif api_response.status_code >= 300:
            code = api_response.status_code
            log.error(f"Connect responded with code {code}")

            if code == 400:
                self.lcd_printer.enqueue_400()
            elif code == 401:
                self.lcd_printer.enqueue_401()
            elif code == 403:
                self.lcd_printer.enqueue_403()
            elif code == 501:
                self.lcd_printer.enqueue_501()

    def determine_command(self, api_response):
        try:
            data = api_response.json()
        except JSONDecodeError:
            log.exception(
                f"Failed to decode a response {api_response}")
        else:
            if data["command"] == "SEND_INFO":
                self.run_command(RespondWithInfo, api_response)
            elif data["command"] == "START_PRINT":
                self.run_command(StartPrint, api_response)
            elif data["command"] == "STOP_PRINT":
                self.run_command(StopPrint, api_response)
            elif data["command"] == "PAUSE_PRINT":
                self.run_command(PausePrint, api_response)
            elif data["command"] == "RESUME_PRINT":
                self.run_command(ResumePrint, api_response)
            elif data["command"] == "RESET_PRINTER":
                self.run_command(ResetPrinter, api_response)
            else:
                command_id = get_command_id(api_response)
                self.connect_api.emit_event(EmitEvents.REJECTED, command_id,
                                            "Unknown command")

    def run_command(self, command_class: Type[Command], api_response,
                    **kwargs):
        self.command_runner.run(command_class, api_response, **kwargs)

    # --- Signal handlers ---

    def telemetry_gathered(self, sender, telemetry):
        self.model.telemetry = telemetry

    def ip_updated(self, sender, local_ip):
        self.model.local_ip = local_ip

        if local_ip is not NO_IP:
            self.lcd_printer.enqueue_message(f"{local_ip}", duration=0)
        else:
            self.lcd_printer.enqueue_message(f"WiFi disconnected", duration=0)

    def storage_updated(self, sender, tree):
        self.model.file_tree = tree

    def sd_state_changed(self, sender, sd_state):
        self.model.sd_state = sd_state

    def state_changed(self, sender: StateManager, command_id=None, source=None):
        state = sender.current_state
        job_id = sender.get_job_id()
        self.model.state = state
        self.connect_api.emit_event(EmitEvents.STATE_CHANGED,
                                    state=state.name, command_id=command_id,
                                    source=source, job_id=job_id)

    def job_id_updated(self, sender, job_id):
        self.model.job_id = job_id

    def connection_error(self, sender, path, json_dict):
        log.debug(f"Connection failed while sending data to the api point "
                  f"{path}. Data: {json_dict}")
        self.lcd_printer.enqueue_connection_failed(
            self.ip_updater.local_ip == NO_IP)

    def media_inserted(self, sender, root, files):
        self.connect_api.emit_event(EmitEvents.MEDIUM_INSERTED, root=root,
                                    files=files)

    def media_ejected(self, sender, root):
        self.connect_api.emit_event(EmitEvents.MEDIUM_EJECTED, root=root)

    # --- Telemetry sending ---

    def keep_sending_telemetry(self):
        run_slowly_die_fast(lambda: self.running, TIME.QUIT_INTERVAL,
                            TIME.TELEMETRY_SEND_INTERVAL, self.send_telemetry)

    def send_telemetry(self):
        delay = time() - self.last_sent_telemetry
        if delay > 2:
            log.warning(f"Something blocked telemetry sending for {delay}")
        self.last_sent_telemetry = time()
        telemetry = self.model.telemetry

        try:
            api_response = self.connect_api.send_model("/p/telemetry",
                                                       telemetry)
        except RequestException:
            log.debug("Failed sending telemetry")
            pass
        else:
            self.handle_telemetry_response(api_response)
