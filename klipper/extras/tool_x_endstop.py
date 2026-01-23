import logging

# Virtual X endstop, using a tool attached X endstop in a toolchanger setup.
# Tool X endstop change may be done either via SET_ACTIVE_TOOL_X_ENDSTOP TOOL=99
# Or via auto-detection of single open tool X endstop via DETECT_ACTIVE_TOOL_X_ENDSTOP
class ToolXEndstopGlobal:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()
        self.tool_x_endstops = {}
        self.last_query = {} # map from tool number to X endstop state
        self.active_x_endstop = None
        self.active_tool_number = -1
        self.gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.mcu_x_endstop = XEndstopRouter(self.printer)

        logging.info("ToolXEndstopGlobal: Initializing and registering chip 'tool_x_endstop'")
        # Register chip to expose tool_x_endstop:x pin
        self.printer.lookup_object('pins').register_chip('tool_x_endstop', self)

        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        # Register commands
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('SET_ACTIVE_TOOL_X_ENDSTOP', self.cmd_SET_ACTIVE_TOOL_X_ENDSTOP,
                                    desc=self.cmd_SET_ACTIVE_TOOL_X_ENDSTOP_help)
        self.gcode.register_command('DETECT_ACTIVE_TOOL_X_ENDSTOP', self.cmd_DETECT_ACTIVE_TOOL_X_ENDSTOP,
                                    desc=self.cmd_DETECT_ACTIVE_TOOL_X_ENDSTOP_help)

    def setup_pin(self, pin_type, pin_params):
        if pin_type != 'endstop':
            raise self.printer.config_error("Tool X endstop virtual pin only useful as endstop pin")
        return self.mcu_x_endstop

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self._detect_active_tool()

    def add_x_endstop(self, config, tool_x_endstop):
        if (tool_x_endstop.tool in self.tool_x_endstops):
            raise config.error("Duplicate tool X endstop nr: %s" % (tool_x_endstop.tool,))
        self.tool_x_endstops[tool_x_endstop.tool] = tool_x_endstop
        self.mcu_x_endstop.add_mcu(tool_x_endstop.mcu_x_endstop)

    def set_active_x_endstop(self, tool_x_endstop):
        if self.active_x_endstop == tool_x_endstop:
            return
        self.active_x_endstop = tool_x_endstop
        if self.active_x_endstop:
            self.mcu_x_endstop.set_active_mcu(tool_x_endstop.mcu_x_endstop)
            self.active_tool_number = self.active_x_endstop.tool
        else:
            self.mcu_x_endstop.set_active_mcu(None)
            self.active_tool_number = -1

    def _query_open_tools(self):
        print_time = self.toolhead.get_last_move_time()
        self.last_query.clear()
        candidates = []
        for tool_x_endstop in self.tool_x_endstops.values():
            triggered = tool_x_endstop.mcu_x_endstop.query_endstop(print_time)
            self.last_query[tool_x_endstop.tool] = triggered
            if not triggered:
                candidates.append(tool_x_endstop)
        return candidates

    def _describe_tool_detection_issue(self, candidates):
        if len(candidates) == 1 :
            return 'OK'
        elif len(candidates) == 0:
            return "All X endstops triggered"
        else:
            return  "Multiple X endstops not triggered: %s" % map(lambda p: p.name, candidates)

    def _detect_active_tool(self):
        active_tools = self._query_open_tools()
        if len(active_tools) == 1 :
            self.set_active_x_endstop(active_tools[0])

    cmd_SET_ACTIVE_TOOL_X_ENDSTOP_help = "Set the tool X endstop that will act as the X endstop."
    def cmd_SET_ACTIVE_TOOL_X_ENDSTOP(self, gcmd):
        tool_nr = gcmd.get_int("T")
        if (tool_nr not in self.tool_x_endstops):
            raise gcmd.error("SET_ACTIVE_TOOL_X_ENDSTOP no tool X endstop for tool %d" % (tool_nr))
        self.set_active_x_endstop(self.tool_x_endstops[tool_nr])

    cmd_DETECT_ACTIVE_TOOL_X_ENDSTOP_help = "Detect which tool is active by identifying an X endstop that is NOT triggered"
    def cmd_DETECT_ACTIVE_TOOL_X_ENDSTOP(self, gcmd):
        active_tools = self._query_open_tools()
        if len(active_tools) == 1 :
            active = active_tools[0]
            gcmd.respond_info("Found active tool X endstop: %s" % (active.name))
            self.set_active_x_endstop(active)
        else:
            self.set_active_x_endstop(None)
            gcmd.respond_info(self._describe_tool_detection_issue(active_tools))

    def get_status(self, eventtime):
        return {
            'last_query': self.last_query,
            'active_tool_number': self.active_tool_number,
            'active_tool_x_endstop': self.active_x_endstop.name if self.active_x_endstop else None
        }

# Routes commands to the selected tool X endstop.
class XEndstopRouter:
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
        raise self.printer.command_error("Cannot interact with X endstop - no active tool X endstop.")




class ToolXEndstop:
    def __init__(self, config):
        self.tool = config.getint('tool')
        self.printer = config.get_printer()
        self.name = config.get_name()
        
        # Parse X endstop pin
        ppins = self.printer.lookup_object('pins')
        pin = config.get('pin')
        ppins.allow_multi_use_pin(pin.replace('^', '').replace('!', ''))
        pin_params = ppins.lookup_pin(pin, can_invert=True, can_pullup=True)
        mcu = pin_params['chip']
        self.mcu_x_endstop = mcu.setup_pin('endstop', pin_params)

        #Register with the global X endstop handler
        self.global_x_endstop = self.printer.load_object(config, "tool_x_endstop")
        self.global_x_endstop.add_x_endstop(config, self)

def load_config(config):
    return ToolXEndstopGlobal(config)

def load_config_prefix(config):
    return ToolXEndstop(config)
