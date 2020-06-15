"""Jablotron Sensor platform

 HA forum    : https://community.home-assistant.io/t/jablotron-ja-80-series-and-ja-100-series-alarm-integration/113315/
 Github repo : https://github.com/plaksnor/HASS-JablotronSystem

 The code contains 2 classes:
 - DeviceScanner() is scanning for packets with sensor data
 - JablotronSensor() is representing a binary_sensor object in HA

 The Jablotron data (for at least the JA-100 series) consists of two important type of packets which are getting send by the alarm system.

 -----------------------------------------------------------------------------------
 The packets starting with d8 08 seem to contain some kind of status report.
 These packets contain on/off data for 1 or more sensors

 For example:
  1  2  3  4  5  6  7  8   9 10 11 12 13 14 15 16  <====================== byte number
 d8 08 00 00 00 00 00 00  00 00 00 10 14 55 00 10  |.............U..|    : nothing is activated
 d8 08 00 00 01 00 00 00  00 00 55 09 00 88 00 02  |..........U.....|    : one or multiple devices has been activated

 byte number:
  4 and  5 = accumulated sensor ID's of devices which are ON. See hextobin() function for decoding. This data is not being used right now.
------------ the next bytes are not used, but already deciphered
 11 and 12 = if 55 09, a specific sensor recently caused this d8 packet
        14 = specific on/off status of a sensor which has changed state
 15 and 16 = specific sensor ID of sensor which has changed state


 -----------------------------------------------------------------------------------
 The packets starting with 55 09 also seem to contain sensor data, but they are only getting send when there has been send a d8 or 55 packet in the last 30 seconds.
 These packets contain on/off data for only 1 sensor, not multiple

 For example:
  1  2  3  4  5  6  7  8   9 10 11 12 13 14 15 16  <====================== byte number
 55 09 00 8a 00 02 40 cc  d2 3b 13 00 0b 00 00 00  |U.....@..;......|    : sensor 00 02 became inactive (8a)
 55 09 00 80 80 01 60 cc  f2 3b 14 00 14 55 00 10  |U.....`..;...U..|    : sensor 80 01 became active (80)

 byte number:
         4 = status (on/off) of device which has changed state
  5 and  6 = specific sensor ID of sensor which has changed state

 -----------------------------------------------------------------------------------

 Recent discoveries
 55 08 = wired    (unconfirmed)
 55 09 = wireless (unconfirmed)

"""

import logging
import binascii
import sys
import re
import time
import asyncio
import threading
import voluptuous as vol
import os

from . import DOMAIN

from concurrent.futures import ThreadPoolExecutor
from homeassistant.helpers.entity import Entity
from homeassistant.components.binary_sensor import (
    PLATFORM_SCHEMA,
    BinarySensorEntity,
    DEVICE_CLASSES_SCHEMA,
)
from homeassistant.const import (
    STATE_ON,
    STATE_OFF,
    CONF_NAME,
    CONF_DEVICE_CLASS
)
import homeassistant.components.sensor as sensor
import homeassistant.helpers.config_validation as cv

from homeassistant import util
from homeassistant.config import load_yaml_config_file, async_log_exception
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType, HomeAssistantType
from homeassistant.util.yaml import dump
#Add MQTT
from homeassistant.components import mqtt

_LOGGER = logging.getLogger(__name__)

devices = []
YAML_DEVICES = 'jablotron/jablotron_devices.yaml'
LOG_INFO = 'jablotron/jablotron.log'

async def async_setup_platform(hass: HomeAssistantType, config: ConfigType, async_add_entities, discovery_info=None):
    if not os.path.exists(hass.config.path('jablotron')):
        os.makedirs(hass.config.path('jablotron'))
    yaml_path = hass.config.path(YAML_DEVICES)    
    devices = await async_load_config(yaml_path, hass, config, async_add_entities)
    data = DeviceScanner(hass, config, async_add_entities, devices)


class JablotronSensor(BinarySensorEntity):
    """Representation of a Sensor."""

    def __init__(self, hass: HomeAssistantType, dev_id: str, name: str, device_class: DEVICE_CLASSES_SCHEMA):
        self._hass = hass
        self._name = 'Jablotron sensor'
        self._state = STATE_OFF
        self.dev_id = dev_id
        self.dev_name = name
        self.dev_class = device_class
        _LOGGER.info('JablotronSensor.__init__(): dev_id created: %s name: %s class: %s', self.dev_id, self.dev_name, self.dev_class)

    @property
    def is_on(self):
        if self._state == STATE_OFF:
            return False
        elif self._state == STATE_ON:
            return True

    @property
    def unique_id(self):
        return self.dev_id

    @property
    def name(self):
        """Return the name of the sensor."""
        if self.dev_name != '':
            return self.dev_name
        else:
            return self.dev_id

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def device_class(self):
        return self.dev_class

    async def async_seen(self, state: str = None):
        """Mark the device as seen."""
        if self._state != state:
            self._state = state

            _LOGGER.debug('JablotronSensor.async_seen(): state updated to %s', state)




class DeviceScanner():
    """ Read configuration and serial port and check for incoming data"""

    def __init__(self, hass, config, async_add_entities, devices):
        self._state = None
        self._sub_state = None
        self._file_path = hass.data[DOMAIN]['port']
        self._available = False
        self._f = None
        self._hass = hass
        self._config = config
        self._model = 'Unknown'
        self._lock = threading.BoundedSemaphore()
        self._stop = threading.Event()
        self._data_flowing = threading.Event()
        self._async_add_entities = async_add_entities
        self.devices = {dev.dev_id: dev for dev in devices}
        self._is_updating = asyncio.Lock()
        self._activation_packet = b''
        self._mode = '55'

        """ default binary strings for comparing states in d8 packets """
        self._old_bin_string = '0'.zfill(32)
        self._new_bin_string = '0'.zfill(32)

        """Since MQTT is run on separate instance I will connect directly"""        
        self._mqtt_enabled = True
        _LOGGER.info("(__init__) MQTT enabled? %s", self._mqtt_enabled)
        
        if self._mqtt_enabled:
          self._mqtt = hass.components.mqtt
          self._data_topic = hass.data[DOMAIN]['data_topic']



        _LOGGER.info('DeviceScanner.__init__(): serial port: %s', format(self._file_path))

        switcher = {
            "0": b'\x30',
            "1": b'\x31',
            "2": b'\x32',
            "3": b'\x33',
            "4": b'\x34',
            "5": b'\x35',
            "6": b'\x36',
            "7": b'\x37',
            "8": b'\x38',
            "9": b'\x39'
        }

        try:

            """ generate activation packet containing the alarm code, to trigger the right sensor packets """
            packet_code = b''
            for c in hass.data[DOMAIN]['code']:
                packet_code = packet_code + switcher.get(c)
            self._activation_packet = b'\x80\x08\x03\x39\x39\x39' + packet_code

            hass.bus.async_listen('homeassistant_stop', self.shutdown_threads)

            self._io_pool_exc = ThreadPoolExecutor(max_workers=5)
            self._read_loop_future = self._io_pool_exc.submit(self._read_loop)
            self._watcher_loop_keepalive_future = self._io_pool_exc.submit(self._watcher_loop_keepalive)
            self._watcher_loop_triggersensorupdate_future = self._io_pool_exc.submit(self._watcher_loop_triggersensorupdate)


        except Exception as ex:
            _LOGGER.error('Unexpected error 1: %s', format(ex) )

    def shutdown_threads(self, event):
        _LOGGER.debug('DeviceScanner.shutdown_threads: handle_shutdown() called' )
        self._stop.set()
        _LOGGER.debug('DeviceScanner.shutdown_threads: exiting handle_shutdown()' )

    @property
    def name(self):
        """Return the name of the DeviceScanner."""
        return 'Jablotron scanner'

    @property
    def state(self):
        """Return the state of the DeviceScanner."""
        return self._state

    @property
    def available(self):
        """Return the availability of incoming data of the DeviceScanner."""
        return self._available



    def _watcher_loop_keepalive(self):
        """Trigger keepalive message to get d8 08 packets."""
        while not self._stop.is_set():
            if not self._data_flowing.wait(0.5):
                self._keepalive()
            else:
                time.sleep(1)

    def _watcher_loop_triggersensorupdate(self):
        """Trigger authentication message to get 55 09 packets."""
        while not self._stop.is_set():
            if not self._data_flowing.wait(0.5):
                self._triggersensorupdate()
            else:
                time.sleep(10)

    def _read_loop(self):
        """Read incoming data"""
        try:
            while not self._stop.is_set():

                self._f = open(self._file_path, 'rb', 64)
                new_state = self._read()

                self._f.close()
                time.sleep(0.5)

        except Exception as ex:
            _LOGGER.error('DeviceScanner._read_loop(): Unexpected error: %s', format(ex) )

        finally:
            _LOGGER.debug('DeviceScanner._read_loop(): Exiting _read_loop()' )

    # function to transform a hex string into a binary string
    def _hextobin(self, hexstring):
        dec = int.from_bytes(hexstring, byteorder=sys.byteorder) # turn to 'little' if sys.byteorder is wrong
        bin_dec = bin(dec)
        binstring = bin_dec[2:]
        binstring = binstring.zfill(32)
        revstring = binstring [::-1]
        return revstring



    async def async_see(self, dev_id: str = None, state: str = None):
        """Create binary sensor.
        This method is a coroutine.
        """

        dev_id = cv.slug(str(dev_id).lower())
        device = self.devices.get(dev_id)

        """State received of already known device"""
        if device:
            await device.async_seen(state)
            await device.async_update_ha_state()
            return

        """State received of unknown device, default device class is motion"""
        dev_id = util.ensure_unique_string(dev_id, self.devices.keys())        
        device = JablotronSensor(self._hass, dev_id, 'unknown', 'motion')
        self.devices[dev_id] = device

        await device.async_seen(state)

        """Update known_devices.yaml"""
        self._hass.async_create_task(
            self.async_update_config(
                self._hass.config.path(YAML_DEVICES), dev_id, device)
        )

        self._async_add_entities([device])
        _LOGGER.info('DeviceScanner.async_see(): added entity %s', device)
        


    async def async_update_config(self, path, dev_id, device):
        """Add device to YAML configuration file.
        This method is a coroutine.
        """
        async with self._is_updating:
            await self._hass.async_add_executor_job(
                update_config, self._hass.config.path(YAML_DEVICES),
                dev_id, device)

    def _read(self):
        """Read incoming data on port"""
        try:
            while True:

                """Try to read data"""
                self._data_flowing.clear()
                packet = self._f.read(64)
                self._data_flowing.set()

                if not packet:
                    _LOGGER.warn("PortScanner._read(): No packets")
                    self._available = False
                    return 'No Signal'

                self._state = True

                """If data can be read, scan for specific incoming packets"""
                if packet[:2] == b'\xd8\x08':

                    _LOGGER.debug('PortScanner._read(): d8 08 packet, part 1: %s', str(binascii.hexlify(packet[0:8]), 'utf-8'))
                    _LOGGER.debug('PortScanner._read(): d8 08 packet, part 2: %s', str(binascii.hexlify(packet[8:16]), 'utf-8'))

                    byte3 = packet[2:3]  # 3rd byte unknown, always 00
                    byte4 = packet[3:4]  # 4th byte, last part of id
                    byte5 = packet[4:5]  # 5th byte, first part of id

                    """Decode sensor ID from 4th and 5th byte, create a binary string and compare this with the last generated binary string. 0 = OFF, 1 = ON"""
                    self._new_bin_string = self._hextobin(byte4+byte5)
                    _LOGGER.debug('PortScanner._read(): old_bin_string: %s', self._old_bin_string)
                    _LOGGER.debug('PortScanner._read(): new_bin_string: %s', self._new_bin_string)

                    for idx, (x, y) in enumerate(zip(self._old_bin_string, self._new_bin_string)):
                      
                        """Continue for devices which has been changed to ON or OFF."""
                        if x != y:

                            dev_id = 'jablotron_' + str(idx)
                            entity_id = 'binary_sensor.' + dev_id

                            if y == '1':
                                _device_state = STATE_ON
                            else:
                                _device_state = STATE_OFF

                            """Only create or update a sensor when this packet is the first d8 08 packet received since startup,
                               or if d8 08 packet reports about 1 specific device (by containing a 55 packet) or,
                               or if a specific device is not active anymore (y == '0')"""
                            if self._mode == 'd8' or (self._mode == '55' and (self._available == False or (y == '1' and packet[10:11] == b'\x55') or y == '0')):

                                """ Create or update sensor """
                                self._hass.add_job(
                                    self.async_see(dev_id, _device_state)
                                )

                    """Retain last binary string"""
                    _LOGGER.debug('PortScanner._read(): updating bin string to %s', self._new_bin_string)
                    self._old_bin_string = self._new_bin_string

                    """Set available to True since we know which devices are ON"""
                    self._available = True


                elif self._mode == '55' and packet[:2] in (b'\x55\x08', b'\x55\x09'):

                    _LOGGER.debug('PortScanner._read(): %s packet, part 1: %s', str(binascii.hexlify(packet[0:2]), 'utf-8'), str(binascii.hexlify(packet[0:8]), 'utf-8'))
                    _LOGGER.debug('PortScanner._read(): %s packet, part 2: %s', str(binascii.hexlify(packet[0:2]), 'utf-8'), str(binascii.hexlify(packet[8:16]), 'utf-8'))

                    packetpart = packet[0:10]

                    byte3 = packetpart[2:3]  # 3rd byte, ??
                    byte4 = packetpart[3:4]  # 4th byte, state of device
                    byte5 = packetpart[4:5]  # 5th byte, first part of device ID
                    byte6 = packetpart[5:6]  # 6th byte, second part of device ID                    
                    _LOGGER.debug('Sensor ID: %s%s : State: %s', str(binascii.hexlify(byte5), 'utf-8'), str(binascii.hexlify(byte6), 'utf-8'), str(binascii.hexlify(byte4), 'utf-8') )
                    log = "device: %s%s : state: %s" % (str(binascii.hexlify(byte5), 'utf-8'), str(binascii.hexlify(byte6), 'utf-8'), str(binascii.hexlify(byte4), 'utf-8'))
                    write_log(self._hass, log)

#                    _LOGGER.info('State: %s', str(binascii.hexlify(byte4), 'utf-8') )
                    
                    """Only process specific state changes"""
                    if byte3 in (b'\x00', b'\x01', b'\x80'):
						# Added 80 för upstairs
                        if byte4 in (b'\x6c', b'\x70', b'\x74', b'\x78', b'\x7c', b'\x80', b'\x84', b'\x88', b'\x8c'):
						# 6c Groventre Dörr     (6e) 4000
                        # 70 Förrådet           (72) 8000
                        # 74 Huvudentre         (76) C000
						# 78 Kontoret           (7a) 0001
                        # 7c Lillhallen         (7e) 4001
                        # 80 Huvudentre Dörr    (82) 8001
                        # 84 Sovrum             (86) C001
                        # 88 vardagsrummet      (8a) 0002
						# 8c Hallen ovan        (8e) 4002
						
                            _device_state = STATE_ON
                        else:
                            _device_state = STATE_OFF

                        """Decode sensor ID from 5th and 6th byte"""
                        dec = int.from_bytes(byte5+byte6, byteorder=sys.byteorder) # turn to 'little' if sys.byteorder is wrong
                        #_LOGGER.info('dec value: %s', str(dec))
                        i = int(dec/64)
                        #_LOGGER.info('Decode sensor: %s', str(i))
                        dev_id = 'jablotron_' + str(i)
                        entity_id = 'binary_sensor.' + dev_id
                        """ Create or update sensor """
                        self._hass.add_job(
                            self.async_see(dev_id, _device_state)
                        )

                    # Added based on panel and app states, needs to be evalutaed and thought of
                    # om man skickar disarm/armed_away/armed_home
                    elif byte3 in (b'\xae', b'\x0c', b'\x2e'):
                        _LOGGER.info('State: %s', translate_hex(str(binascii.hexlify(byte3), 'utf-8')))
                        #om användaren är från app
                        if byte4 in (b'\x6c', b'\x70', b'\x74'):
                            _LOGGER.info('APP')
                            _LOGGER.info('user: %s', translate_hex(str(binascii.hexlify(byte4), 'utf-8')))
                        elif byte4 in (b'\x6d', b'\x71', b'\x76'):
                            _LOGGER.info('Panel')
                            _LOGGER.info('user: %s', translate_hex(str(binascii.hexlify(byte4), 'utf-8')))
                        else:
                            _LOGGER.info('New unknown user: %s', str(binascii.hexlify(byte4), 'utf-8'))
                        
                        if self._mqtt_enabled:                            
                            payload = '{"state":"%s","panel":"%s%s","user":"%s"}' % ( str(binascii.hexlify(byte3), 'utf-8'), str(binascii.hexlify(byte5), 'utf-8'), str(binascii.hexlify(byte5), 'utf-8'), str(binascii.hexlify(byte4), 'utf-8'))
                            _LOGGER.info("Sending MQTT message with APP")
                            self._mqtt.publish(self._data_topic, payload , retain=True)

                    else:
                        _LOGGER.info("New unknown %s packet: %s %s %s %s", str(binascii.hexlify(packet[0:2]), 'utf-8'), str(binascii.hexlify(byte3), 'utf-8'), str(binascii.hexlify(byte4), 'utf-8'), str(binascii.hexlify(byte5), 'utf-8'), str(binascii.hexlify(byte6), 'utf-8'))
                        _LOGGER.info('PortScanner._read(): %s packet, part 1: %s', str(binascii.hexlify(packet[0:2]), 'utf-8'), str(binascii.hexlify(packet[0:8]), 'utf-8'))
                        _LOGGER.info('PortScanner._read(): %s packet, part 2: %s', str(binascii.hexlify(packet[0:2]), 'utf-8'), str(binascii.hexlify(packet[8:16]), 'utf-8'))
                    
                else:
#                    log = "Unknown packet: %s" % str(binascii.hexlify(packet))
#                    write_log(self._hass, log)
                    pass
#                    _LOGGER.info("Unknown packet: %s", packet)
#                    self._stop.set()

        except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError):
            _LOGGER.warning("PortScanner._read(): File or data not present at the moment: %s", self._file_path)
            return 'Failed'

        except Exception as ex:
            _LOGGER.error('PortScanner._read(): Unexpected error 3: %s', format(ex) )
            return 'Failed'

        return state

    def _sendPacket(self, packet):
        f = open(self._file_path, 'wb')
        f.write(packet)
        time.sleep(0.1) # lower reliability without this delay
        f.close()

    def _triggersensorupdate(self):
        """ Send trigger for sensor update to system"""


        self._sendPacket(self._activation_packet)
        self._sendPacket(b'\x52\x02\x13\x05\x9a')

        # Sending OFF signal
        for dev_id, device in self.devices.items():
            self._hass.add_job(
                self.async_see(dev_id, STATE_OFF)
            )

    def _keepalive(self):
        """ Send keepalive to system"""
        self._sendPacket(b'\x52\x01\x02')



async def async_load_config(path: str, hass: HomeAssistantType, config: ConfigType, async_add_entities):
    """Load devices from YAML configuration file.
    This method is a coroutine.
    """
    dev_schema = vol.Schema({
        vol.Required('dev_id'): cv.string,
        vol.Optional(CONF_NAME, default=''): cv.string,
        vol.Optional(CONF_DEVICE_CLASS, default='motion'): DEVICE_CLASSES_SCHEMA
#        vol.Optional(CONF_ICON, default=None): vol.Any(None, cv.icon),
#        vol.Optional('track', default=False): cv.boolean,
#        vol.Optional(CONF_MAC, default=None):
#        vol.Any(None, vol.All(cv.string, vol.Upper)),
#        vol.Optional(CONF_AWAY_HIDE, default=DEFAULT_AWAY_HIDE): cv.boolean,
#        vol.Optional('gravatar', default=None): vol.Any(None, cv.string),
#        vol.Optional('picture', default=None): vol.Any(None, cv.string),
#        vol.Optional(CONF_CONSIDER_HOME, default=consider_home): vol.All(
#            cv.time_period, cv.positive_timedelta),
    })
    result = []
    try:
        _LOGGER.debug("async_load_config(): reading config file %s", path)

        devices = await hass.async_add_job(
            load_yaml_config_file, path)

        _LOGGER.debug('async_load_config(): devices loaded from config file: %s', devices)
       
    except HomeAssistantError as err:
        _LOGGER.error("async_load_config(): unable to load %s: %s", path, str(err))
        return []
    except FileNotFoundError as err:
        _LOGGER.debug("async_load_config(): file %s could not be found: %s", path, str(err))
        return []


    for dev_id, device in devices.items():
        # Deprecated option. We just ignore it to avoid breaking change
#        device.pop('vendor', None)
        try:
            device = dev_schema(device)
            device['dev_id'] = cv.slugify(dev_id)      
        except vol.Invalid as exp:
            async_log_exception(exp, dev_id, devices, hass)
        else:           
            _LOGGER.debug('device: %s', device)
            dev = JablotronSensor(hass, **device)
            result.append(dev)

            """ Create sensors for each device in devices """
#            device = JablotronSensor(hass, dev_id)
            async_add_entities([dev])        
    return result

def update_config(path: str, dev_id: str, device: JablotronSensor):
    """Add device to YAML configuration file."""

    with open(path, 'a') as out:
        device = {device.dev_id: {
            'dev_id': device.dev_id,
#            ATTR_NAME: device._name,
#            ATTR_MAC: sensor.mac,
#            ATTR_ICON: sensor.icon,
#            'picture': sensor.config_picture,
#            'track': sensor.track,
#            CONF_AWAY_HIDE: sensor.away_hide,
        }}
        out.write('\n')
        out.write(dump(device))
    _LOGGER.debug('update_config(): updated %s with sensor %s', path, dev_id)

def write_log(hass, log: str):
    # Converting datetime object to string
    secondsSinceEpoch = time.time()
    timeObj = time.localtime(secondsSinceEpoch)
    timestampStr = '%d-%02d-%02d %02d:%02d:%02d' % (timeObj.tm_year, timeObj.tm_mon, timeObj.tm_mday, timeObj.tm_hour, timeObj.tm_min, timeObj.tm_sec)
    
    log = "%s : %s" % (timestampStr, log)
    path = hass.config.path(LOG_INFO)
    with open(path, 'a') as out:
        out.write('\n')
        out.write(log)
        
def translate_hex(hex: str):
    if hex == '6c' or hex == '6d':
        return "Johan"
    elif hex == '70' or hex == '71':
        return "Sandra"
    elif hex == '74' or hex == '75':
        return "Casper"
    elif hex == 'ae':
        return "armed_home"
    elif hex == '2e':
        return "armed_away"
    elif hex == '0c':
        return "disarm"
    else:
        return "unknown"