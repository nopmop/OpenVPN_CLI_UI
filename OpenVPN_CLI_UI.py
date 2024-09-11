import logging, os, subprocess, signal, time, asyncio
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll, Container
from textual.widgets import Header, Footer, Label, Input, Button, Static, Placeholder, DataTable
from textual.reactive import reactive
from textual import events
from textual.keys import Keys

# Config
CONFIG_LOGFILE = "/var/log/openvpn_ui.log"
CONFIG_OPENVPN_LOGFILE = "/var/log/openvpn.log"
CONFIG_OPENVPN_CONFIG_DIR = "/etc/openvpn/config/"
CONFIG_OPENVPN_AUTH_USER_PASS = "/etc/openvpn/config/secret"
CONFIG_OPENVPN_UP_SCRIPT = "/etc/openvpn/config/up.cmd"
CONFIG_OPENVPN_DOWN_SCRIPT = "/etc/openvpn/config/down.cmd"
CONFIG_CYCLE_TIME = 3600  # Time in seconds for cycle mode

logging.basicConfig(filename=CONFIG_LOGFILE, level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

class OpenVPN_CLI_UI(App):
    CSS_PATH = "styles.tcss"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.openvpn_process = None
        self.mode = "fixed"
        self.selected_config = reactive(None)
        self.cycle_time = CONFIG_CYCLE_TIME  # Time in seconds for cycle mode
        self.next_cycle_in = self.cycle_time

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        mode = Button(label="Mode\n(CTRL-A)", id="mode_button")
        start = Button(label="Start/Stop\n(CTRL-S)", id="start_button")
        cycle = Button(label="Cycle\n(CTRL-X)", id="cycle_button")
        kill = Button(label="Kill\n(CTRL-K)", id="kill_button")
        quit = Button(label="Quit\n(CTRL-Q)", id="quit_button")

        yield Horizontal(
                Vertical(mode, start, kill, cycle, quit, classes="panel_buttons"),
                self.create_config_panel(),
            classes="panel_container_top")
        yield Container(
            self.create_process_panel(),
            Horizontal(
                self.create_log_panel(), 
                self.create_ui_log_panel(),
            ),  # Create both log panels side by side
        classes="panel_container_bottom")

    def create_config_panel(self):
        try:
            self.config_files = sorted(set(f for f in os.listdir(CONFIG_OPENVPN_CONFIG_DIR) if f.endswith(".ovpn")))
            if not self.config_files:
                logging.warning("No config files found in /etc/openvpn/config/")
        except Exception as e:
            logging.error(f"Error loading config files from {CONFIG_OPENVPN_CONFIG_DIR}: {e}")
            self.config_files = []

        # Create DataTable instead of Select
        self.config_table = DataTable()

        # Add a column for the configuration filenames
        self.config_table.add_column("Available Configs")
        
        # Store row keys for future reference
        self.row_keys = []

        for config_file in self.config_files:
            row_key = self.config_table.add_row(config_file)
            self.row_keys.append(row_key)  # Store the key


        # Initially select the first configuration file
        if self.config_files:
            self.selected_config = self.config_files[0]
            self.config_table.focused = True  # Focus the table initially
        

        return Vertical(
            Vertical(
                Container(Label("Mode: ", classes="bold"), Label("fixed", id="mode_label"), classes="horizontal"),
                Container(Label("Config: ", classes="bold"), Label("", id="config_label"), classes="horizontal"),
                Container(Label("Cycle period: ", classes="bold"), Label(f"---", id="cycle_label"), classes="horizontal"),
                Container(Label("Next cycle in: ", classes="bold"), Label(f"{self.next_cycle_in}s", id="next_cycle_in"), classes="horizontal"),
                classes="panel_runtime_info"
            ),
            Horizontal(
            self.config_table,  # Replaced Select with DataTable
            classes="panel_datatable"
            ),
            classes="panel_config"
        )

    def move_to_row(self, search_value: str):
        """Find the row with the given value and move to that row."""
        row_index = self.find_row_index(search_value)
        logging.info(f"For {search_value} found row_index: {row_index}")
        if row_index != -1:
            self.move_to_row_with_index(row_index)
        else:
            logging.info(f"Value {search_value} not found in the table")

    def find_row_index(self, search_value: str) -> int:
        """Finds the index of the row that contains the search_value in the first column."""
        for row_index, row_key in enumerate(self.row_keys):
            row_data = self.config_table.get_row(row_key)
            
            # Assuming you're searching for the value in the first column of the row
            if row_data[0] == search_value:
                return row_index
        
        logging.info(f"Value {search_value} not found in the table")
        return -1  # Return -1 if the value is not found

    def move_to_row_with_index(self, row_index: int):
        """Programmatically move the cursor to a specific row and ensure it's visible."""
        # Move the cursor to the desired row
        self.config_table.move_cursor(row=row_index, scroll=True)
        
        # Optionally log or take action with the newly selected row
        self.selected_config = self.config_files[row_index]
        logging.info(f"Moved to row {row_index}, selected config: {self.selected_config}")
        
    async def on_key(self, event: events.Key) -> None:
        if event.key == Keys.Tab:
            # Handle TAB key to switch focus between the DataTable and Input
            self.config_table.focused = True
            self.config_table.focus()
        if event.key == Keys.Enter:
            # Handle ENTER key to select the row in DataTable
            if self.config_table.focused:
                selected_row = self.config_table.cursor_row
                if selected_row is not None:
                    self.selected_config = self.config_files[selected_row]
                    self.handle_start_stop()

        elif event.key == Keys.ControlA:
            logging.info("Hotkey pressed: CTRL-A")
            self.handle_mode()
        elif event.key == Keys.ControlS:
            logging.info("Hotkey pressed: CTRL-S")
            self.handle_start_stop()
        elif event.key == Keys.ControlX:
            logging.info("Hotkey pressed: CTRL-X")
            if self.mode == "cycle":
                self.handle_cycle()
        elif event.key == Keys.ControlK:
            logging.info("Hotkey pressed: CTRL-K")
            self.handle_kill()
        elif event.key == Keys.ControlQ:
            logging.info("Hotkey pressed: CTRL-Q")
            await self.handle_quit()

    async def handle_config_switch(self):
        # Switch to fixed mode
        self.mode = "fixed"
        mode_label = self.get_widget_by_id("mode_label")
        mode_label.update(self.mode)
        self.stop_openvpn()
        self.start_openvpn(self.selected_config)

    def start_openvpn(self, config_file):
        config_path = os.path.join(CONFIG_OPENVPN_CONFIG_DIR, config_file)
        try:
            command = ["/usr/sbin/openvpn", "--config", config_path, "--auth-user-pass", CONFIG_OPENVPN_AUTH_USER_PASS, "--log", CONFIG_OPENVPN_LOGFILE, "--up", CONFIG_OPENVPN_UP_SCRIPT, "--down", CONFIG_OPENVPN_DOWN_SCRIPT, "--script-security", "2"]
            self.openvpn_process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            config_label = self.get_widget_by_id("config_label")
            config_label.update(self.selected_config)
            self.move_to_row(self.selected_config)
            logging.info(f"Executing: {' '.join(command)}")
        except Exception as e:
            logging.error(f"Failed to start OpenVPN ({' '.join(command)}): {e}")

    def stop_openvpn(self):
        if self.openvpn_process:
            self.openvpn_process.terminate()
            self.openvpn_process.wait()
            self.openvpn_process = None
            config_label = self.get_widget_by_id("config_label")
            config_label.update("")
            logging.info("Stopped OpenVPN")

    def create_process_panel(self):
        self.process_label = Static("OpenVPN process details will appear here.")
        return Vertical(Label("OpenVPN Process", classes="bold"), self.process_label, classes="panel_process")

    def create_log_panel(self):
        self.log_output = Static()
        self.log_output_container = Vertical(
            Label("OpenVPN Log", classes="bold"),
            VerticalScroll(self.log_output)
        )
        return self.log_output_container

    def create_ui_log_panel(self):
        self.ui_log_output = Static()
        self.ui_log_output_container = Vertical(
            Label("UI Log", classes="bold"),
            VerticalScroll(self.ui_log_output)
        )
        return self.ui_log_output_container

    async def on_mount(self) -> None:
        asyncio.create_task(self.update_process_panel())  # Use asyncio to manage the background task
        asyncio.create_task(self.monitor_log_file(CONFIG_OPENVPN_LOGFILE, self.log_output, self.log_output_container))  # Start monitoring the OpenVPN log file
        asyncio.create_task(self.monitor_log_file(CONFIG_LOGFILE, self.ui_log_output, self.ui_log_output_container))  # Start monitoring the UI log file
        asyncio.create_task(self.cycle_timer())  # Start the cycling timer task
        self.update_next_cycle_in()
        self.config_table.focus()

    async def cycle_timer(self):
        """Periodically check if in cycle mode, update remaining time, and call handle_cycle."""
        try:
            while True:
                if self.mode == "cycle" and self.openvpn_process:
                    self.next_cycle_in = self.cycle_time
                    while self.next_cycle_in > 0:
                        await asyncio.sleep(1)
                        self.next_cycle_in -= 1
                        self.update_next_cycle_in()
                    self.handle_cycle()
                else:
                    await asyncio.sleep(1)  # Avoid unnecessary looping when not in cycle mode
        except Exception as e:
            logging.error(f"Error in cycle_timer: {e}")

    def update_next_cycle_in(self):
        """Update the remaining time label in the UI."""
        remaining_time_label = self.get_widget_by_id("next_cycle_in")
        if self.mode == "fixed":
            remaining_time_label.update("---")
        else:
            remaining_time_label.update(f"{self.next_cycle_in}s")

    def handle_mode(self):
        if self.mode == "fixed":
            self.mode = "cycle"
            mode_label = self.get_widget_by_id("cycle_label")
            mode_label.update(f"{self.cycle_time}s")
        elif self.mode == "cycle":
            self.mode = "fixed"
            mode_label = self.get_widget_by_id("cycle_label")
            mode_label.update(f"---")
        mode_label = self.get_widget_by_id("mode_label")
        mode_label.update(self.mode)
            
    def handle_start_stop(self):
        if self.openvpn_process:
            self.stop_openvpn()
        if self.selected_config:
            self.start_openvpn(self.selected_config)

    def handle_cycle(self):
        if self.mode == "cycle" and self.openvpn_process:
            self.stop_openvpn()
            self.kill_openvpn() # just in case
            selected_config_idx = self.config_files.index(self.selected_config)
            next_config_idx = (selected_config_idx + 1) % len(self.config_files)
            next_config = self.config_files[next_config_idx]
            self.selected_config = next_config
            self.start_openvpn(next_config)

    def handle_kill(self):
        if self.openvpn_process:
            self.kill_openvpn()

    async def handle_quit(self):
        if self.openvpn_process:
            self.kill_openvpn()
        await self.shutdown()

    def shutdown(self):
        logging.info("Shutting down OpenVPN UI")
        return exit()

    def kill_openvpn(self):
        if self.openvpn_process:
            os.kill(self.openvpn_process.pid, signal.SIGKILL)
            self.openvpn_process = None
            config_label = self.get_widget_by_id("config_label")
            config_label.update("")
            logging.info("Killed OpenVPN process")

    async def update_process_panel(self):
        while True:
            process_info = self.get_openvpn_process_info()
            self.process_label.update(process_info)
            await asyncio.sleep(1)  # Update every second

    def get_openvpn_process_info(self):
        try:
            result = subprocess.run(["/bin/sh -c 'ps auxw | grep openvpn | grep -v grep'"], capture_output=True, text=True, shell=True)
            process_info = result.stdout.strip() if result.returncode == 0 else "No OpenVPN process found."
        except Exception as e:
            process_info = f"Error retrieving OpenVPN process info: {e}"
            logging.error(process_info)

        return process_info

    async def monitor_log_file(self, logfile, output_widget, scrollable):
        """Monitors a log file and updates the corresponding widget with the last 10 lines."""
        try:
            while True:
                with open(logfile, "r") as f:
                    lines = f.readlines()
                    if lines:
                        # Take the last 10 lines
                        last_lines = lines[-10:]
                        # Update the widget content and ensure the last line is at the bottom
                        output_widget.update("".join(last_lines))
                        scrollable.scroll_end(animate=False)  # Ensure the last line is at the bottom
                await asyncio.sleep(1)  # Wait a bit before checking again
        except Exception as e:
            logging.error(f"Error monitoring log file {logfile}: {e}")

if __name__ == "__main__":
    app = OpenVPN_CLI_UI()
    app.run()
