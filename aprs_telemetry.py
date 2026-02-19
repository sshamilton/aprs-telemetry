#!/usr/bin/env python3

import time
import serial
import smbus2  # <-- Import the smbus2 library
import os
import subprocess
import logging # <-- NEW: Import logging module
from logging.handlers import RotatingFileHandler # <-- NEW: For log rotation

# --- APRS Configuration ---
CALLSIGN = "W2KGY"  # Updated to your callsign
SSID = "2"          # Updated to your SSID

# --- NEW: Timing Configuration ---
INITIAL_SEND_INTERVAL_SEC = 60    # 1 minute (This is now the main loop poll time)
REGULAR_SEND_INTERVAL_SEC = 1800  # 30 minutes
# INITIAL_SEND_COUNT = 3 (REMOVED - Replaced by new logic)
METADATA_INTERVAL_SEC = 86400     # 24 hours (24*60*60)
VOLTAGE_DELTA_TRIGGER = 5.0       # Trigger send if voltage changes by this much

# --- Serial Port Configuration ---
# SERIAL_PORT = "/dev/ttyUSB0" # <-- REMOVED
BAUD_RATE = 9600 # Check your radio's packet/data speed setting
POTENTIAL_PORTS = [f"/dev/ttyUSB{i}" for i in range(4)] # Check USB0, 1, 2, 3

# --- Sensor Configuration ---
# Use I2C bus 1 for Raspberry Pi (standard for pins 3 & 5)
I2C_BUS = 1
BATTERY_SENSOR_ADDRESS = 0x40 # Address for Battery INA226
SOLAR_SENSOR_ADDRESS = 0x44   # Address for Solar INA226
LIGHT_SENSOR_ADDRESS = 0x41   # NEW: Address for Light INA226 (check your jumpers!)

# --- INA226 Register Definitions ---
INA226_REG_CONFIG = 0x00
INA226_REG_BUSVOLTAGE = 0x02
# Config: Avg 1, 1.1ms bus conversion, 1.1ms shunt conversion, continuous
INA226_CONFIG_DEFAULT = 0x4127 
# LSB for Bus Voltage is 1.25mV
VOLTAGE_LSB = 0.00125

# --- Pi Temperature File ---
TEMP_FILE_PATH = "/sys/class/thermal/thermal_zone0/temp"

# --- NEW: Sequence Number File ---
SEQUENCE_FILE = "aprs_seq.txt" # File to store the last sequence number

# --- NEW: Log File ---
LOG_FILE = "aprs_sensor.log"
LOG_MAX_BYTES = 1024 * 1024 * 5  # 5 MB
LOG_BACKUP_COUNT = 5

# --- Calculated APRS Values ---
METADATA_ADDRESSEE = f"{CALLSIGN}-{SSID}".ljust(9) # Pad callsign to 9 chars for metadata

# --- UPDATED: Fixed Location Packet Payload ---
# Using '\' as separator and 'L' as symbol (Lighthouse)
LOCATION_PACKET_PAYLOAD = r"=4119.63N\07400.62WL Trail of the Fallen="

# --- Global Variables ---
sequence_number = 0
i2c_bus = None # Make I2C bus global for simplicity in this example
ser = None # Make serial port global to be managed by functions
last_metadata_send_time = 0 # Timer for metadata refresh
last_regular_send_time = 0  # NEW: Timer for regular 30-min send
last_battery_voltage = 0.0  # NEW: Last known battery voltage
last_solar_voltage = 0.0    # NEW: Last known solar voltage
last_light_voltage = 0.0    # NEW: Last known light voltage

# --- Radio Re-configuration Function ---
def re_initialize_radio_mode(ser):
    """Sends commands to put an already-in-cmd-mode radio into packet mode."""
    try:
        # --- MODIFIED: Updated command ---
        logging.info("Sending U APK003 VIA WIDE1-1,WIDE2-1...")
        ser.write(b"U APK003 VIA WIDE1-1,WIDE2-1\r") # Use \r only
        time.sleep(1)
        ser.read(ser.in_waiting or 100) # Clear buffer

        logging.info("Sending k (packet mode)...")
        ser.write(b"k\r") # Use \r only
        time.sleep(1)
        ser.flushInput() # Clear any response
        logging.info("Radio should be in packet mode.")
        return True
    except Exception as e:
        logging.error(f"Error re-initializing radio: {e}")
        return False

# --- Low-level Helper Function to Send Packet PAYLOAD ---
def send_aprs_payload(ser, payload_string):
    """Checks radio mode, re-configures if needed, then sends payload."""
    # global ser <-- THIS LINE WAS THE ERROR AND HAS BEEN REMOVED
    try:
        # 1. Check if radio has reset to command mode
        logging.info("Checking radio mode...")
        ser.flushInput()
        ser.write(b"\r") # Use \r only
        time.sleep(0.5) # Wait for a "cmd:" prompt
        
        response_bytes = ser.read(ser.in_waiting or 100)
        response = response_bytes.decode('utf-8', 'ignore')

        if "cmd:" in response.lower():
            logging.warning(f"Radio reset to command mode detected. Re-configuring...")
            if not re_initialize_radio_mode(ser):
                logging.error("Failed to re-configure radio. Aborting send.")
                return False
            # If re-config was successful, radio is now in packet mode
        else:
            logging.info("Radio mode OK (packet mode).")

        # 2. Send the actual packet payload
        logging.info(f"Sending Payload: {payload_string}")
        packet_bytes = (payload_string + "\r").encode('utf-8') # Use \r only
        ser.write(packet_bytes)
        ser.flush() # Ensure data is sent immediately
        
        return True
        
    except serial.SerialException as e:
        logging.error(f"Serial Error during send: {e}")
        # Let the main loop's exception handler manage this
        raise e 
    except Exception as e:
        logging.error(f"Error sending payload: {e}")
        return False

# --- Function to Get Voltage ---
def get_sensor_voltage(bus, sensor_address):
    """Reads the bus voltage from a specific INA226 sensor via smbus."""
    global INA226_REG_BUSVOLTAGE, VOLTAGE_LSB
    try:
        # Read 2 bytes (16 bits) from the bus voltage register
        raw_voltage_bytes = bus.read_i2c_block_data(sensor_address, INA226_REG_BUSVOLTAGE, 2)
        
        # Convert the two bytes (MSB first) into a 16-bit integer
        raw_voltage = int.from_bytes(raw_voltage_bytes, byteorder='big')
        
        # Convert the raw integer value to voltage
        voltage = raw_voltage * VOLTAGE_LSB
        
        logging.info(f"Read Voltage from {hex(sensor_address)}: {voltage:.2f} V (Raw: {raw_voltage})")
        return voltage
    except OSError as e:
        logging.warning(f"I2C Sensor Read Error at {hex(sensor_address)}: {e}. Check wiring. Returning 3.44")
        return 3.44 # <-- MODIFIED: Return placeholder on I2C error
    except Exception as e:
        logging.error(f"Unexpected error reading sensor {hex(sensor_address)}: {e}. Returning 3.44")
        return 3.44 # <-- MODIFIED: Return placeholder on other error

# --- Function to Get CPU Temperature ---
def get_cpu_temperature():
    """Reads the Pi's CPU temperature from the system file."""
    global TEMP_FILE_PATH
    try:
        with open(TEMP_FILE_PATH, 'r') as f:
            temp_str = f.read().strip()
        # Value is in millicelcius (e.g., 45678), divide by 1000
        temp_c = int(temp_str) / 1000.0
        logging.info(f"Read CPU Temp: {temp_c:.1f} C")
        return temp_c
    except FileNotFoundError:
        logging.error(f"Error: Temperature file not found at {TEMP_FILE_PATH}. Returning 0.0")
        return 0.0 # <-- MODIFIED: Return placeholder
    except Exception as e:
        logging.error(f"Error reading CPU temp: {e}. Returning 0.0")
        return 0.0 # <-- MODIFIED: Return placeholder

# --- NEW Function to Get CPU Load ---
def get_cpu_load():
    """Gets the 1-minute CPU load average."""
    try:
        # os.getloadavg() returns (1-min, 5-min, 15-min) load
        load_1m = os.getloadavg()[0]
        logging.info(f"Read CPU Load (1m): {load_1m:.2f}")
        return load_1m
    except Exception as e:
        logging.error(f"Error reading CPU load: {e}. Returning 0.0")
        return 0.0 # <-- MODIFIED: Return placeholder

# --- NEW Function to Get Throttle Status ---
def get_throttle_status():
    """
    Checks the Pi's throttle status using vcgencmd.
    Returns an 8-bit string.
    Bit 0 (right-most): Under-voltage has occurred
    Bit 1: Throttling has occurred
    """
    try:
        result = subprocess.run(['vcgencmd', 'get_throttled'], capture_output=True, text=True, check=True)
        # Output is "throttled=0x50000"
        hex_val = result.stdout.split('=')[1]
        status = int(hex_val, 16)

        # 8-bit string, all set to '0' initially
        bits = ['0'] * 8 

        # Bit 0: Under-voltage (past)
        if (status & 0x1):
            bits[0] = '1'
        # Bit 1: Throttled (past)
        if (status & 0x2):
            bits[1] = '1'
        # Bit 16: Under-voltage (NOW)
        if (status & 0x10000):
            bits[0] = '1' # Also set bit 0 if happening now
        # Bit 17: Throttled (NOW)
        if (status & 0x20000):
            bits[1] = '1' # Also set bit 1 if happening now

        # Reverse bits for APRS order (b7b6b5b4b3b2b1b0) and join
        bit_string = "".join(reversed(bits))
        logging.info(f"Read Throttle Status: 0b{bit_string} (Raw: {hex_val.strip()})")
        return bit_string

    except FileNotFoundError:
        logging.warning("Error: 'vcgencmd' not found. This script must run on a Raspberry Pi.")
        return "00000000" # Return all-zero as a fallback
    except Exception as e:
        logging.error(f"Error reading throttle status: {e}")
        return "00000000" # Return all-zero as a fallback

# --- NEW: Function to save sequence number ---
def save_sequence_number(seq_num):
    """Saves the current sequence number to a file."""
    global SEQUENCE_FILE
    try:
        with open(SEQUENCE_FILE, 'w') as f:
            f.write(str(seq_num))
        logging.info(f"Saved sequence number {seq_num} to {SEQUENCE_FILE}")
    except IOError as e:
        logging.error(f"Error: Could not write to sequence file {SEQUENCE_FILE}: {e}")

# --- NEW: Function to load sequence number ---
def load_sequence_number():
    """Loads the last sequence number from a file and sets the next one."""
    global sequence_number, SEQUENCE_FILE
    try:
        with open(SEQUENCE_FILE, 'r') as f:
            last_seq = int(f.read().strip())
        sequence_number = (last_seq + 1) % 1000 # Start at the *next* number
        logging.info(f"Loaded sequence number {last_seq}, starting next at {sequence_number}")
    except (FileNotFoundError, ValueError, IOError) as e:
        logging.warning(f"Could not read sequence file ({e}). Starting at sequence 0.")
        sequence_number = 0

# --- Function to Send Telemetry ---
def send_telemetry(ser, voltage, solar_voltage, light_voltage, cpu_temp, cpu_load, throttle_bits, seq_num):
    """Formats and sends the APRS telemetry packet PAYLOAD."""
    global sequence_number # Allow modification of the global sequence number

    # --- MODIFIED: Removed 'if None in [...]' check ---
    
    # Format Telemetry Payload Body
    seq_str = str(seq_num).zfill(3)
    
    # A1: VBat
    vbat_str = f"{voltage:.1f}"
    
    # A2: VSolar
    vsolar_str = f"{solar_voltage:.1f}"
    
    # A3: VLight
    vlight_str = f"{light_voltage:.1f}"

    # A4: CPUTemp
    a4_temp_str = f"{cpu_temp:.1f}"

    # A5: CPULoad
    a5_load_str = f"{cpu_load:.2f}" # Send with 2 decimal places

    # Digital bits (already an 8-char string)
    digital_bits = throttle_bits

    # The payload starts directly with T#
    telemetry_payload = f"T#{seq_str},{vbat_str},{vsolar_str},{vlight_str},{a4_temp_str},{a5_load_str},{digital_bits}"

    success = send_aprs_payload(ser, telemetry_payload)
    if success:
         sequence_number = (seq_num + 1) % 1000 # Increment and wrap
         save_sequence_number(sequence_number) # <-- SAVE after successful send
    else:
        logging.error("Telemetry payload send failed.")
    return success


# --- Function to Send Metadata ---
def send_metadata(ser):
    """Sends the initial APRS telemetry metadata packet PAYLOADS."""
    global LOCATION_PACKET_PAYLOAD # <-- NEW: Need to access this global
    logging.info("Sending APRS Telemetry Metadata Payloads...")
    
    # Updated to add CPULoad to A5 and add BITS packet
    metadata_payloads = [
        f":{METADATA_ADDRESSEE}:PARM.VBat,VSolar,VLight,CPUTemp,CPULoad",
        f":{METADATA_ADDRESSEE}:UNITS.V,V,V,C,Load",
        # EQNS: (a,b,c) for 5 channels.
        # All are 0,1,0 (Raw value is correct)
        f":{METADATA_ADDRESSEE}:EQNS.0,1,0,0,1,0,0,1,0,0,1,0,0,1,0",
        # NEW BITS packet to label bits 0 (UV) and 1 (THROT)
        f":{METADATA_ADDRESSEE}:BITS.UV,THROT,,,,,,,",
        # NEW: Add the fixed location packet to the metadata burst
        LOCATION_PACKET_PAYLOAD
    ]
    
    retries = 0
    max_retries = 3
    while retries < max_retries:
        success_count = 0
        for i, payload in enumerate(metadata_payloads):
            # Pass 'ser' to send_aprs_payload
            if send_aprs_payload(ser, payload):
                success_count += 1
                logging.info("Waiting 7 seconds before next payload...")
                time.sleep(7)
            else:
                logging.error(f"Failed to send metadata payload #{i+1}. Retrying...")
                time.sleep(5)
                break 
        if success_count == len(metadata_payloads):
            logging.info("Metadata payloads sent successfully.")
            return True 
        else:
            retries += 1
            logging.warning(f"Metadata send attempt {retries}/{max_retries} failed. Retrying in 10s...")
            time.sleep(10)
    logging.error("Failed to send initial metadata after multiple retries.")
    return False 

# --- REMOVED initial_radio_setup FUNCTION ---

# --- NEW: Function to find and initialize radio ---
def find_and_setup_radio():
    """Scans potential USB ports, finds the D72, and sets it to packet mode."""
    global POTENTIAL_PORTS, BAUD_RATE
    
    for port in POTENTIAL_PORTS:
        if not os.path.exists(port):
            continue # Skip if port file doesn't exist
            
        logging.info(f"Checking port {port} for radio...")
        temp_ser = None
        try:
            temp_ser = serial.Serial(port, BAUD_RATE, timeout=2)
            time.sleep(2) # Give port time to open
            
            logging.info(f"Forcing radio on {port} to command mode with Ctrl+C...")
            temp_ser.flushInput()
            temp_ser.write(b"\x03") # Send Ctrl+C
            time.sleep(1) # Wait for radio to respond
            
            response_bytes = temp_ser.read(temp_ser.in_waiting or 100)
            response = response_bytes.decode('utf-8', 'ignore')
            logging.info(f"Radio response on {port}: '{response.strip()}'")

            if "cmd:" in response.lower():
                logging.info(f"Radio found on {port}! Configuring for packet mode...")
                if re_initialize_radio_mode(temp_ser):
                    logging.info(f"Radio on {port} successfully configured.")
                    return temp_ser # Return the working serial object
                else:
                    logging.error(f"Found radio on {port}, but failed to configure.")
                    temp_ser.close()
            else:
                logging.info(f"Device on {port} is not the correct radio. Closing.")
                temp_ser.close()
                
        except serial.SerialException as e:
            logging.warning(f"Could not open or test port {port}: {e}")
            if temp_ser and temp_ser.is_open:
                temp_ser.close()
        except Exception as e:
            logging.error(f"Unexpected error while checking {port}: {e}")
            if temp_ser and temp_ser.is_open:
                temp_ser.close()
                
    logging.warning("Could not find radio on any port.")
    return None


# --- Main Program ---
if __name__ == "__main__":
    
    # --- NEW: Setup Logging ---
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # File Handler
    try:
        # --- MODIFIED: Use RotatingFileHandler ---
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
        file_handler.setFormatter(log_formatter)
    except IOError as e:
        print(f"CRITICAL: Could not open log file {LOG_FILE}: {e}")
        # We can't use logging yet, so just print and exit
        exit()

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)

    # Get root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO) # Set the minimum level to log
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    # --- END NEW ---
    
    logging.info("--- APRS Telemetry Script Started ---")
    
    # --- NEW: Load last sequence number ---
    load_sequence_number()

    # --- Initialize I2C and INA226 Sensor ---
    try:
        logging.info(f"Initializing I2C bus {I2C_BUS}...")
        i2c_bus = smbus2.SMBus(I2C_BUS)
        logging.info("I2C Bus Initialized.")
        
        config_bytes = list(INA226_CONFIG_DEFAULT.to_bytes(2, byteorder='big'))

        # --- Configure BATTERY Sensor (0x40) ---
        logging.info(f"Initializing INA226 sensor at {hex(BATTERY_SENSOR_ADDRESS)}...")
        i2c_bus.write_i2c_block_data(BATTERY_SENSOR_ADDRESS, INA226_REG_CONFIG, config_bytes)
        logging.info(f"INA226 at {hex(BATTERY_SENSOR_ADDRESS)} configured.")
        battery_voltage = get_sensor_voltage(i2c_bus, BATTERY_SENSOR_ADDRESS)
        if battery_voltage == 3.44: # Check against our failure value
            raise OSError(f"Initial read failed for sensor at {hex(BATTERY_SENSOR_ADDRESS)}.")
        logging.info(f"Battery Sensor OK. Initial voltage read: {battery_voltage:.2f} V")

        # --- Configure SOLAR Sensor (0x44) ---
        logging.info(f"Initializing INA226 sensor at {hex(SOLAR_SENSOR_ADDRESS)}...")
        i2c_bus.write_i2c_block_data(SOLAR_SENSOR_ADDRESS, INA226_REG_CONFIG, config_bytes)
        logging.info(f"INA226 at {hex(SOLAR_SENSOR_ADDRESS)} configured.")
        solar_voltage = get_sensor_voltage(i2c_bus, SOLAR_SENSOR_ADDRESS)
        if solar_voltage == 3.44:
            raise OSError(f"Initial read failed for sensor at {hex(SOLAR_SENSOR_ADDRESS)}.")
        logging.info(f"Solar Sensor OK. Initial voltage read: {solar_voltage:.2f} V")

        # --- Configure LIGHT Sensor (0x41) ---
        logging.info(f"Initializing INA226 sensor at {hex(LIGHT_SENSOR_ADDRESS)}...")
        i2c_bus.write_i2c_block_data(LIGHT_SENSOR_ADDRESS, INA226_REG_CONFIG, config_bytes)
        logging.info(f"INA226 at {hex(LIGHT_SENSOR_ADDRESS)} configured.")
        light_voltage = get_sensor_voltage(i2c_bus, LIGHT_SENSOR_ADDRESS)
        if light_voltage == 3.44:
            raise OSError(f"Initial read failed for sensor at {hex(LIGHT_SENSOR_ADDRESS)}.")
        logging.info(f"Light Sensor OK. Initial voltage read: {light_voltage:.2f} V")


    except FileNotFoundError:
        logging.critical(f"Error: I2C bus {I2C_BUS} not found.")
        logging.critical("Make sure I2C is enabled with 'sudo raspi-config'.")
        exit()
    except OSError as e:
        logging.critical(f"I2C Error. At least one sensor not found. Check wiring and addresses.")
        logging.critical(f"Error details: {e}")
        exit()
    except Exception as e:
        logging.critical(f"Error initializing sensors: {e}", exc_info=True)
        exit()

    # --- MODIFIED: Initialize Serial Port and Configure Radio ---
    ser = None
    while ser is None:
        try:
            ser = find_and_setup_radio()
            if ser is None:
                logging.warning(f"No radio found. Waiting 60 seconds to re-scan...")
                time.sleep(60)
        except Exception as e:
            logging.error(f"Error during radio search: {e}. Retrying in 60s...")
            time.sleep(60)
    logging.info("Radio connection established.")


    # --- Send Metadata Payloads Once ---
    # global last_metadata_send_time <-- THIS LINE WAS THE ERROR AND IS REMOVED
    if not send_metadata(ser):
         logging.error("Exiting due to metadata send failure.")
         if ser and ser.is_open:
              ser.close()
         if i2c_bus:
              i2c_bus.close()
         exit()
    else:
        # Start the 24-hour timer *after* successful send
        last_metadata_send_time = time.time()

    # --- NEW: Send first packet and initialize last_known values ---
    try:
        logging.info("--- Sending First Telemetry Packet ---")
        last_battery_voltage = get_sensor_voltage(i2c_bus, BATTERY_SENSOR_ADDRESS)
        last_solar_voltage = get_sensor_voltage(i2c_bus, SOLAR_SENSOR_ADDRESS)
        last_light_voltage = get_sensor_voltage(i2c_bus, LIGHT_SENSOR_ADDRESS)
        cpu_temp = get_cpu_temperature()
        cpu_load = get_cpu_load()
        throttle_bits = get_throttle_status()
        
        send_telemetry(ser, last_battery_voltage, last_solar_voltage, last_light_voltage, cpu_temp, cpu_load, throttle_bits, sequence_number)
        last_regular_send_time = time.time()

    except Exception as e:
        logging.critical(f"Failed to send first telemetry packet: {e}", exc_info=True)
        # Continue anyway, it will try again in the loop
    
    # --- Main Loop ---
    try:
        logging.info(f"Initial send complete. Entering main 60-second polling loop...")
        while True:
            # --- MODIFIED: Main loop logic ---
            
            # 1. Check if serial port is alive. If not, find it.
            if ser is None:
                logging.warning("Radio connection lost. Re-scanning...")
                ser = find_and_setup_radio()
                if ser is None:
                    logging.warning("Failed to find radio. Waiting 60s...")
                    time.sleep(INITIAL_SEND_INTERVAL_SEC)
                    continue # Skip to next loop iteration to try finding port again
                logging.info("Radio re-connected.")

            # 2. If port is alive, do the work
            try:
                # --- Check for Metadata Refresh ---
                if (time.time() - last_metadata_send_time) > METADATA_INTERVAL_SEC:
                    logging.info("--- 24-Hour Metadata Refresh Triggered ---")
                    if send_metadata(ser):
                        last_metadata_send_time = time.time()
                    else:
                        logging.warning("Metadata refresh failed. Will retry next cycle.")
                        # If it fails, ser might be None, exception handler will catch it

                # --- Read Current Sensor Values ---
                current_battery_voltage = get_sensor_voltage(i2c_bus, BATTERY_SENSOR_ADDRESS)
                current_solar_voltage = get_sensor_voltage(i2c_bus, SOLAR_SENSOR_ADDRESS)
                current_light_voltage = get_sensor_voltage(i2c_bus, LIGHT_SENSOR_ADDRESS)
                
                # --- Check for Triggers ---
                time_for_regular_send = (time.time() - last_regular_send_time) > REGULAR_SEND_INTERVAL_SEC
                
                bat_delta = abs(current_battery_voltage - last_battery_voltage)
                solar_delta = abs(current_solar_voltage - last_solar_voltage)
                light_delta = abs(current_light_voltage - last_light_voltage)
                
                voltage_trigger = (bat_delta > VOLTAGE_DELTA_TRIGGER or 
                                   solar_delta > VOLTAGE_DELTA_TRIGGER or 
                                   light_delta > VOLTAGE_DELTA_TRIGGER)

                # --- Send Logic ---
                if time_for_regular_send or voltage_trigger:
                    if time_for_regular_send:
                        logging.info("--- 30-Minute Regular Send Triggered ---")
                    if voltage_trigger and not time_for_regular_send:
                        logging.info(f"--- Voltage Delta Triggered (Bat:{bat_delta:.1f}V, Sol:{solar_delta:.1f}V, Lgt:{light_delta:.1f}V) ---")
                    
                    # Read remaining sensors only if we are sending
                    cpu_temp = get_cpu_temperature()
                    cpu_load = get_cpu_load()
                    throttle_bits = get_throttle_status()

                    # Send the telemetry packet
                    if send_telemetry(ser, current_battery_voltage, current_solar_voltage, current_light_voltage, cpu_temp, cpu_load, throttle_bits, sequence_number):
                        # On success, update all "last known" values
                        last_regular_send_time = time.time()
                        last_battery_voltage = current_battery_voltage
                        last_solar_voltage = current_solar_voltage
                        last_light_voltage = current_light_voltage
                else:
                    # --- MODIFIED: Removed noisy 1-min log message ---
                    pass
            
            except serial.SerialException as e:
                 logging.error(f"Serial Error in main loop: {e}. Closing port.")
                 if ser: ser.close()
                 ser = None # This will trigger the find_and_setup_radio() block on the next loop
            except Exception as e:
                logging.critical(f"An unexpected error occurred in main loop: {e}", exc_info=True)
                logging.info("Waiting 30s before retrying...")
                time.sleep(30) # Wait a bit before retrying on general errors
            
            # Sleep for 1 minute at the *end* of the loop
            logging.info(f"Waiting {INITIAL_SEND_INTERVAL_SEC} seconds...")
            time.sleep(INITIAL_SEND_INTERVAL_SEC)

    except KeyboardInterrupt:
        logging.info("Ctrl+C detected. Exiting...")
    
    finally:
        # --- Cleanup ---
        # Save the last sequence number one final time
        save_sequence_number(sequence_number)
        if ser and ser.is_open:
            ser.close()
            logging.info("Serial port closed.")
        if i2c_bus:
            i2c_bus.close()
            logging.info("I2C bus closed.")
        logging.info("--- APRS Telemetry Script Finished ---")
