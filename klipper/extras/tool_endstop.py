
import logging

# Virtual endstop, using a tool attached endstop in a toolchanger setup.
# Tool endstop change may be done either via SET_ACTIVE_TOOL_ENDSTOP TOOL=99
# Or via auto-detection of single open tool endstop via DETECT_ACTIVE_TOOL_ENDSTOP
class ToolEndstopGlobal:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        self.tool_endstops = {}
        self.last_query = {} # map from tool number to endstop state
        self.active_endstop = None
        self.active_tool_number = -1
        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.mcu_endstop = EndstopRouter(self.printer)

        # Register chip to expose tool_endstop:x pin
        self.printer.lookup_object('pins').register_chip('tool_endstop', self)

        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('SET_ACTIVE_TOOL_ENDSTOP', self.cmd_SET_ACTIVE_TOOL_ENDSTOP,
                                    desc=self.cmd_SET_ACTIVE_TOOL_ENDSTOP_help)
        self.gcode.register_command('DETECT_ACTIVE_TOOL_ENDSTOP', self.cmd_DETECT_ACTIVE_TOOL_ENDSTOP,
                                    desc=self.cmd_DETECT_ACTIVE_TOOL_ENDSTOP_help)

    def setup_pin(self, pin_type, pin_params):
        if pin_type != 'endstop':
            raise self.printer.config_error("Tool endstop virtual pin only useful as endstop pin")
        return self.mcu_endstop

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self._detect_active_tool()

    def add_endstop(self, config, tool_endstop):
        if (tool_endstop.tool in self.tool_endstops):
            raise config.error("Duplicate tool endstop nr: %s" % (tool_endstop.tool,))
        self.tool_endstops[tool_endstop.tool] = tool_endstop
        self.mcu_endstop.add_mcu(tool_endstop.mcu_endstop)

    def set_active_endstop(self, tool_endstop):
        if self.active_endstop == tool_endstop:
            return
        self.active_endstop = tool_endstop
        if self.active_endstop:
            self.mcu_endstop.set_active_mcu(tool_endstop.mcu_endstop)
            self.active_tool_number = self.active_endstop.tool
        else:
            self.mcu_endstop.set_active_mcu(None)
            self.active_tool_number = -1

    def _query_open_tools(self):
        print_time = self.toolhead.get_last_move_time()
        self.last_query.clear()
        candidates = []
        for tool_endstop in self.tool_endstops.values():
            triggered = tool_endstop.mcu_endstop.query_endstop(print_time)
            self.last_query[tool_endstop.tool] = triggered
            if not triggered:
                candidates.append(tool_endstop)
        return candidates

    def _describe_tool_detection_issue(self, candidates):
        if len(candidates) == 1 :
            return 'OK'
        elif len(candidates) == 0:
            return "All endstops triggered"
        else:
            return  "Multiple endstops not triggered: %s" % map(lambda p: p.name, candidates)

    def _detect_active_tool(self):
        active_tools = self._query_open_tools()
        if len(active_tools) == 1 :
            self.set_active_endstop(active_tools[0])

    cmd_SET_ACTIVE_TOOL_ENDSTOP_help = "Set the tool endstop that will act as the endstop."
    def cmd_SET_ACTIVE_TOOL_ENDSTOP(self, gcmd):
        tool_nr = gcmd.get_int("T")
        if (tool_nr not in self.tool_endstops):
            raise gcmd.error("SET_ACTIVE_TOOL_ENDSTOP no tool endstop for tool %d" % (tool_nr))
        self.set_active_endstop(self.tool_endstops[tool_nr])

    cmd_DETECT_ACTIVE_TOOL_ENDSTOP_help = "Detect which tool is active by identifying an endstop that is NOT triggered"
    def cmd_DETECT_ACTIVE_TOOL_ENDSTOP(self, gcmd):
        active_tools = self._query_open_tools()
        if len(active_tools) == 1 :
            active = active_tools[0]
            gcmd.respond_info("Found active tool endstop: %s" % (active.name))
            self.set_active_endstop(active)
        else:
            self.set_active_endstop(None)
            gcmd.respond_info(self._describe_tool_detection_issue(active_tools))

    def get_status(self, eventtime):
        return {
            'last_query': self.last_query,
            'active_tool_number': self.active_tool_number,
            'active_tool_endstop': self.active_endstop.name if self.active_endstop else None
        }

# Routes commands to the selected tool endstop.
class EndstopRouter:
    def __init__(self, printer):
        self.active_mcu = None
        self.set_active_mcu(None)
        self._mcus = []
        self._steppers = []
        self.printer = printer

    def add_mcu(self, mcu_endstop):
        self._mcus.append(mcu_endstop)
        for s in self._steppers:
            mcu_endstop.add_stepper(s)

    def set_active_mcu(self, mcu_endstop):
        self.active_mcu = mcu_endstop
        # Update Wrappers
        if self.active_mcu:
            self.get_mcu = self.active_mcu.get_mcu
            self.home_start = self.active_mcu.home_start
            self.home_wait = self.active_mcu.home_wait
            self.query_endstop = self.active_mcu.query_endstop
        else:
            self.get_mcu = self.on_error
            self.home_start = self.on_error
            self.home_wait = self.on_error
            self.query_endstop = self.on_error

    def add_stepper(self, stepper):
        self._steppers.append(stepper)
        for m in self._mcus:
            m.add_stepper(stepper)
    def get_steppers(self):
        return list(self._steppers)

    def on_error(self, *args, **kwargs):
        raise self.printer.command_error("Cannot interact with endstop - no active tool endstop.")

    def get_position_endstop(self):
        if not self.active_mcu:
            # This will get picked up by the endstop, and is static
            # Report 0 and fix up in the homing sequence
            return 0.0
        return self.active_mcu.get_position_endstop()


class ToolEndstop:
    def __init__(self, config):
        self.tool = config.getint('tool')
        self.printer = config.get_printer()
        self.name = config.get_name()
        
        # Parse endstop pin
        ppins = self.printer.lookup_object('pins')
        pin = config.get('pin')
        ppins.allow_multi_use_pin(pin.replace('^', '').replace('!', ''))
        pin_params = ppins.lookup_pin(pin, can_invert=True, can_pullup=True)
        mcu = pin_params['chip']
        self.mcu_endstop = mcu.setup_pin('endstop', pin_params)

        #Register with the global endstop handler
        self.global_endstop = self.printer.load_object(config, "tool_endstop_global")
        self.global_endstop.add_endstop(config, self)

def load_config(config):
    return ToolEndstopGlobal(config)

def load_config_prefix(config):
    return ToolEndstop(config)
