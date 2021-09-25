"""Self-watering garden proof of concept

Two relays are used:
    - 1 for a classic pump: Pin 2,
    - 1 for water nebulizers for orchids: Pin 12

Humidity sensor: ADC0

The smallest interval between actions is 1 hour. During this time the ESP is put
into deep sleep.

Flash command:
    $ ampy -p /dev/ttyUSB0 put main.py
Bytecode compilation for ESP8266:
    $ mpy-cross -march=xtensa main.py

    PS: Do not forget to add the following lines to boot.py:
    ```import main
    main.main()```
"""
# Micropython imports
from micropython import const
from machine import Pin, ADC
from machine import RTC, DEEPSLEEP, DEEPSLEEP_RESET, deepsleep, reset_cause
import network

# Standard imports
from time import sleep

# Custom imports
import urequests
import ujson

# Pinout
# Relays are low level triggered: default value to HIGH
PUMP_RELAY = Pin(2, mode=Pin.OUT, pull=Pin.PULL_UP, value=True)
NEBULISATORS_1 = Pin(12, mode=Pin.OUT, pull=Pin.PULL_UP, value=True)
HUMIDITY_SENSOR = ADC(0)

# Network config
ESSID = ""
PASSWORD = ""
LAN_CONFIG = ("192.168.1.77", "255.255.255.0", "192.168.1.1", "192.168.1.3")
REPORT_URL = "http://192.168.1.3/esp8266?"

# Accepted humidity level; if below: pump is triggered
_HUMIDITY_THRESHOLD = const(60)
# Nb of wakeups (in hours) between each trigger
_PUMP_COUNTER = const(6 * 24)  # every day
_NEBULISATORS_COUNTER = const(12)  # every 12hours
# Durations (in seconds)
_NEBULISATORS_DURATION = const(135)  # 2min15
_PUMP_DURATION = const(0)  # 7s Duration for each trigger
_PUMP_INTER_SLEEP = const(0)  # 5 * 60  # Small pause between 2 triggers in a same session
# Misc
_HOUR_MS = const(3600000)  # 60 * 60 * 1000


def get_humidity_percent(value):
    """Get relative humidity as percentage from raw sensor

    .. note:: Calculation of oefficients
        Solve the following system:

            100% = MaxRawValue * x + y
            40% = MinRawValue * x + y

        With MaxRawValue=297 for 100% water, MinRawValue=378 for 0% water
            y = 40 - MinRawValue (60 / (MaxRawValue - MinRawValue))
            x = 60 / (MaxRawValue - MinRawValue)

        ... or just copy code in https://rosettacode.org/wiki/Map_range#Python

    :param value: Raw value from ADC connected to capacitive sensor (voltage measure)
    :type value: <int>
    :rtype: <float>
    """
    return round(-value * 0.741 + 320, 1)


def trigger_relay(relay, duration):
    """Trigger the given relay during some time

    :param relay: Initialized pin
        .. warning:: Expected relays are "active low triggered" !
    :param duration: Time in seconds
    """
    relay.value(False)
    sleep(duration)
    relay.value(True)


def init_network():
    """Setup WiFi connection

    :returns: True if network is successfully configured, False otherwise.
        5 attempts are made before returning False.
    :rtype: <boolean>
    """
    max_attempts = 5
    # ESP as a station to AP
    station = network.WLAN(network.STA_IF)

    station.active(True)
    station.ifconfig(LAN_CONFIG)
    station.connect(ESSID, PASSWORD)

    attempts = 0
    while attempts < max_attempts:
        if station.isconnected():
            return True
        sleep(2)
        attempts += 1
    return False


def send_query(data):
    """Send GET query with given parameters

    .. warning:: Be careful with HTTPS protocol: axtls lib in mircopython
        doesn't support DHE or ECDHE ciphers.
    """
    request = urequests.get(
        REPORT_URL + "&".join([k + "=" + str(v) for k, v in data.items()]),
    )
    # print(request.text)
    request.close()


def deep_sleep(ms_sleep_time):
    """Put the device in deep sleep

    .. warning:: Don't forget to connect GPIO16 to RST for wakeups based on timers.

    :param ms_sleep_time: Duration of deep sleep in milliseconds.
        Should not be above ~3h45 (13612089337μs). See ESP.deepSleepMax().
        Pay attention to time drift of RTC clock due to temperature changes,
        for long periods.
    :type ms_sleep_time: <int>
    """
    # ESP8266 only; for ESP32 use machine.deepsleep(ms_sleep_time)
    # configure RTC.ALARM0 to be able to wake the device
    rtc = RTC()
    rtc.irq(trigger=rtc.ALARM0, wake=DEEPSLEEP)

    # set RTC.ALARM0 to fire after X milliseconds (waking the device)
    rtc.alarm(rtc.ALARM0, ms_sleep_time)

    # put the device to sleep
    deepsleep()


def main():
    """Entry point, handle counters, RTC memory, and trigger relays

    .. note:: For safety purpose, if `machine.reset_cause() != machine.DEEPSLEEP_RESET`,
        the pump counter is set to half the value of _PUMP_COUNTER.
        Thus nebulisator counter is set to 0 for instant triggering.
        Indeed, the RTC memory is reset during a power failure.

        Also, pump trigger doesn't accept to be postponed beyond 5 days.
    """
    # Wait serial line after boot => easier to flash from PC
    sleep(3)

    # Load/reload counters from RTC memory
    rtc = RTC()
    if not rtc.memory():
        print("Init RTC mem")
        pump_counter = _PUMP_COUNTER if reset_cause() == DEEPSLEEP_RESET else int(_PUMP_COUNTER / 2)
        nebulisator_counter = 0
        pump_not_triggered_counter = 0
    else:
        wakeup_counters = ujson.loads(rtc.memory())
        print("Restore from RTC mem:", wakeup_counters)
        pump_counter = wakeup_counters["pump"] - 1
        nebulisator_counter = wakeup_counters["nebulisators"] - 1
        pump_not_triggered_counter = wakeup_counters[
            "pump_not_triggered"]

    # Get soil humidity
    nb_measures = 100
    humidity = get_humidity_percent(
        sum([HUMIDITY_SENSOR.read() for _ in range(nb_measures)]) / nb_measures
    )
    print("Humidity:", humidity)

    # Trigger nebulisators
    if nebulisator_counter <= 0:
        print("Trig nebulisators")
        # Trigger nebulisators
        trigger_relay(NEBULISATORS_1, _NEBULISATORS_DURATION)

        nebulisator_counter = _NEBULISATORS_COUNTER

    # Trigger pump
    if pump_counter <= 0:
        if humidity >= _HUMIDITY_THRESHOLD and pump_not_triggered_counter <= 5:
            # Wake up too soon: Triggering is not necessary
            # => postpone to 1 day
            pump_counter = 24
            pump_not_triggered_counter += 1
        else:
            # Trigger pump 2 times in _PUMP_INTER_SLEEP secs,
            # during _PUMP_DURATION secs each
            print("Trig pump")
            trigger_relay(PUMP_RELAY, _PUMP_DURATION)
            sleep(_PUMP_INTER_SLEEP)
            trigger_relay(PUMP_RELAY, _PUMP_DURATION)

            pump_counter = _PUMP_COUNTER
            pump_not_triggered_counter = 0

    # Sync RTC mem
    wakeup_counters = {
        "pump": pump_counter,
        "nebulisators": nebulisator_counter,
        "pump_not_triggered": pump_not_triggered_counter,
    }
    rtc.memory(ujson.dumps(wakeup_counters))
    print("Save in RTC mem:", wakeup_counters)

    # Send report on network
    if init_network():
        try:
            # Add humidity in the report
            wakeup_counters["HR"] = humidity
            send_query(wakeup_counters)
        except Exception as e:
            # Ugly but absolutely no error should stop the main loop
            print(e)

    # Put mcu in deep sleep
    deep_sleep(_HOUR_MS)


main()
