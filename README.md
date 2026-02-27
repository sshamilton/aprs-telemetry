# **Raspberry Pi APRS Telemetry Station**

A robust, Python-based telemetry station designed for remote deployment (e.g., mountaintops). It monitors power systems via I2C sensors and system health, transmitting data over APRS using a serial-connected radio.

## **Features**

* **Multi-Channel Power Monitoring**: Uses three INA226 sensors to monitor Battery Voltage, Solar Panel Output (\~28V), and Light levels (photocell).  
* **System Health Telemetry**: Monitors Raspberry Pi CPU temperature, 1-minute load average, and throttle status (detects under-voltage or thermal throttling).  
* **Event-Driven Transmission**:  
  * **Regular Intervals**: Sends telemetry every 30 minutes.  
  * **Delta Triggers**: Immediately sends a packet if any voltage changes by more than 5.0V (e.g., solar charging kicks in or lights turn on).  
  * **Startup Burst**: Sends data every minute for the first 3 minutes of operation.  
* **APRS Metadata Support**: Automatically handles PARM, UNIT, EQNS, and BITS packets every 24 hours to ensure APRS-IS and igates correctly label your data.  
* **Fixed Location Beacon**: Includes a compressed location beacon with a Lighthouse symbol and custom comment.  
* **Remote Robustness**:  
  * **Auto-Port Discovery**: Scans /dev/ttyUSB0-3 to find the radio if the device path changes on reboot.  
  * **Sequence Persistence**: Saves the APRS sequence number to disk to avoid duplicate packet rejection after power loss.  
  * **Fail-Safe Readings**: Uses placeholder values (e.g., 3.44V) if a sensor fails, ensuring the script continues to run.  
  * **Log Rotation**: Rotates logs at 5MB (keeping 5 backups) to prevent disk exhaustion.

## **Hardware Requirements**

* **Raspberry Pi** (Any model with I2C and USB).  
* **APRS Radio**: Tested with Kenwood D72 style command sets (TNC2 compatible).  
* **Sensors**: 3x INA226 Voltage/Current sensors.  
  * Address 0x40: Battery  
  * Address 0x44: Solar  
  * Address 0x41: Light  
* **USB-to-Serial Adapter**: To connect the radio to the Pi.

## **Installation**

### **1\. Enable I2C and Hardware Optimization**

Use sudo raspi-config to enable the I2C interface. For power savings, it is recommended to disable Bluetooth, Audio, and HDMI in /boot/config.txt.

### **2\. Install Dependencies**

sudo pip3 install smbus2 pyserial

### **3\. Setup Permissions**

Ensure your user has access to I2C and Serial ports:

sudo usermod \-a \-G dialout,i2c yourusername

## **Configuration**

Edit the variables at the top of aprs\_telemetry.py:

* CALLSIGN: Your amateur radio callsign.  
* SSID: Usually 2 for telemetry stations.  
* LOCATION\_PACKET\_PAYLOAD: Your station coordinates in APRS format.  
* VOLTAGE\_DELTA\_TRIGGER: Sensitivity for immediate event-driven packets.

## **Usage**

Run the script directly or set it up as a systemd service:

python3 aprs\_telemetry.py

Monitor the logs in real-time:

tail \-f aprs\_sensor.log

## **APRS Telemetry Mapping**

| Channel | Parameter | Unit | Description |
| :---- | :---- | :---- | :---- |
| **A1** | VBat | V | Main Battery Bank Voltage |
| **A2** | VSolar | V | Solar Panel Input Voltage |
| **A3** | VLight | V | Light/Photocell sensor voltage |
| **A4** | CPUTemp | C | Raspberry Pi Internal Temperature |
| **A5** | CPULoad | Load | 1-minute system load average |
| **B0** | UV | Bit | Under-voltage detected (Throttle Status) |
| **B1** | THROT | Bit | CPU Throttling detected (Thermal) |

## **License**

This project is intended for use by Amateur Radio operators. Please ensure you comply with your local frequency regulations and licensing requirements.
