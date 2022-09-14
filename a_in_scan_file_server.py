from __future__ import print_function
from flask import Flask, render_template, request, jsonify
import logging
import threading
import time
from os import system
from ctypes import c_double
import os
import datetime
import struct
import keyboard
import traceback
import RPi.GPIO as GPIO

from uldaq import (get_daq_device_inventory, DaqDevice, AInScanFlag, ScanStatus,
                   ScanOption, create_float_buffer, InterfaceType, AiInputMode)

class ButtonEvent():
    def __init__(self, channel, active_low=False):
        self.channel = channel
        self.latch = True
        self.active_low = active_low

    # Return true on the first active press event
    # clear_pressed must be called prior to ensure edge detction
    def is_pressed(self):
        if self.active_low:
            pressed = not GPIO.input(self.channel)
        else:
            pressed = GPIO.input(self.channel)
        if pressed and not self.latch:
            self.latch = True
            return True
        return False

    def clear_pressed(self):
        if self.active_low:
            pressed = not GPIO.input(self.channel)
        else:
            pressed = GPIO.input(self.channel)
        if not pressed and self.latch:
            self.latch = False


class DAQ():
    def __init__(self):
        self.daq_device = None
        self.start_pending = False
        self.stop_pending = False
        self.num_channels = 1
        self.data_rate = 10000
        self.range_index = 0
        self.input_mode = AiInputMode.DIFFERENTIAL
        self.total_buffer_size = 0

    def set_start_pending(self, debug=False):
        if debug:
            print("DAQ: set_start_pending()")
        self.start_pending = True

    def get_start_pending(self, debug=False):
        if debug:
            print("DAQ: get_start_pending() = " + str(self.start_pending))
        start_pending = self.start_pending
        self.start_pending = False
        return start_pending

    def set_stop_pending(self, debug=False):
        if debug:
            print("DAQ: set_stop_pending()")
        self.stop_pending = True

    def get_stop_pending(self, debug=False):
        if debug:
            print("DAQ: get_stop_pending() = " + str(self.stop_pending))
        stop_pending = self.stop_pending
        self.stop_pending = False
        return stop_pending

    # num_channels: integer >= 0
    def set_num_channels(self, num_channels, debug=False):
        if debug:
            print("DAQ: set_num_channels(" + str(num_channels) + ")")
        self.num_channels = num_channels

    def get_num_channels(self, debug=False):
        if debug:
            print("DAQ: get_num_channels() = " + str(self.num_channels))
        return self.num_channels

    # data_rate: Samples/second to be collected for each channel, integer >= 0
    def set_data_rate(self, data_rate, debug=False):
        if debug:
            print("DAQ: set_data_rate(" + str(data_rate) + ")")
        self.data_rate = data_rate

    def get_data_rate(self, debug=False):
        if debug:
            print("DAQ: get_data_rate() = " + str(self.data_rate))
        return self.data_rate

    # range_index: 0 = 10V, 1 = 5V, 2 = 2V, 3 = 1V
    def set_range_index(self, range_index, debug=False):
        if debug:
            print("DAQ: set_range_index(" + str(range_index) + ")")
        self.range_index = range_index

    def get_range_index(self, debug=False):
        if debug:
            print("DAQ: get_range_index() = " + str(self.range_index))
        return self.range_index

    # input_mode: AiInputMode.DIFFERENTIAL, AiInputMode.SINGLE_ENDED
    def set_input_mode(self, input_mode, debug=False):
        if debug:
            print("DAQ: set_input_mode(" + str(input_mode) + ")")
        self.input_mode = input_mode
    
    def get_input_mode(self, debug=False):
        if debug:
            print("DAQ: get_input_mode() = " + str(self.input_mode))
        return self.input_mode
    
    # max_write_speed: maximum Samples/second bandwidth of the destination file
    # device_index: number of the device to use within the list of attached DAQ devices
    def connect(self, max_write_speed, device_index=0, debug=False):
        if debug:
            print("DAQ: connect(max_write_speed=" + str(max_write_speed) + ", device_index=" + str(device_index) + ")")

        # Get descriptors for all available DAQ devices
        devices = get_daq_device_inventory(InterfaceType.ANY)
        number_of_devices = len(devices)
        if number_of_devices == 0:
            raise RuntimeError('Error: No DAQ devices found')
        device_index = min(device_index, number_of_devices-1)
        
        if debug:
            print('Found', number_of_devices, 'DAQ device(s):')
            for i in range(number_of_devices):
                print('  [', i, '] ', devices[i].product_name, ' (',
                    devices[i].unique_id, ')', sep='')
            print("\nUsing DAQ device", device_index)

        # Create the DAQ device from the descriptor at the specified index
        self.daq_device = DaqDevice(devices[device_index])

        # Get the AiDevice object and verify it is valid
        self.ai_device = self.daq_device.get_ai_device()
        if self.ai_device is None:
            raise RuntimeError('Error: The DAQ device does not support analog input')

        # Verify the specified device supports hardware pacing for analog input
        self.ai_info = self.ai_device.get_info()
        if not self.ai_info.has_pacer():
            raise RuntimeError('\nError: The specified DAQ device does not support hardware paced analog input')

        # Establish a connection to the DAQ device
        descriptor = self.daq_device.get_descriptor()
        if debug:
            print('\nConnecting to', descriptor.dev_string, '- please wait...')
        self.daq_device.connect(connection_code=0)

        # Check channel number bounds
        max_channels = self.ai_info.get_num_chans_by_mode(self.input_mode)
        self.num_channels = max(self.num_channels, 1)
        self.num_channels = min(self.num_channels, max_channels)

        # Validate range index
        self.ranges = self.ai_info.get_ranges(self.input_mode)
        if self.range_index >= len(self.ranges):
            self.range_index = len(self.ranges) - 1

        # Check data rate bounds
        min_rate = self.ai_info.get_min_scan_rate()
        max_rate = self.ai_info.get_max_scan_rate()
        max_throughput = self.ai_info.get_max_throughput()
        max_throughput_per_channel = max_throughput / self.num_channels
        max_write_speed_per_channel = max_write_speed / self.num_channels
        self.data_rate = max(self.data_rate, min_rate)
        self.data_rate = min(self.data_rate, max_rate, max_throughput_per_channel, max_write_speed_per_channel)
        self.data_rate = int(self.data_rate)

    def disconnect(self, debug=False):
        if debug:
            print("DAQ: disconnect()")

        if self.daq_device:
            if self.daq_device.is_connected():
                self.daq_device.disconnect()
            self.daq_device.release()
        self.daq_device = None

    def start_scan(self, data_buffer, buffer_size_seconds, debug=False):
        if debug:
            print("DAQ: start_scan(data_buffer, buffer_size_seconds=" + str(buffer_size_seconds) + ")")

        channel_buffer_size = self.data_rate * buffer_size_seconds
        self.total_buffer_size = channel_buffer_size * self.num_channels

        self.data_buffer = data_buffer
        
        # Start the acquisition (Channel 0 up to self.num_channels-1)
        self.ai_device.a_in_scan(0, self.num_channels-1, self.input_mode, self.ranges[self.range_index], 
                                 channel_buffer_size, self.data_rate, ScanOption.CONTINUOUS, AInScanFlag.DEFAULT, 
                                 self.data_buffer)
        self.prev_count = 0
        self.prev_index = 0

        # Wait for the scan to start
        status = ScanStatus.IDLE
        while status == ScanStatus.IDLE:
            status, _ = self.ai_device.get_scan_status()

    def scan_running(self, debug=False):
        scan_running = False
        if self.daq_device:
            status, _ = self.ai_device.get_scan_status()
            scan_running = (status == ScanStatus.RUNNING)
        
        if debug:
            print("DAQ: scan_running() = " + str(scan_running))
        return scan_running

    def stop_scan(self, debug=False):
        if debug:
            print("DAQ: stop_scan()")

        if self.daq_device:
            if self.scan_running():
                self.ai_device.scan_stop()

    # Return the number of unread data values
    def data_available(self, debug=False):
        data_available = 0
        if self.daq_device:
            _, transfer_status = self.ai_device.get_scan_status()
            data_available = (transfer_status.current_total_count - self.prev_count)
        
        if debug:
            print("DAQ: data_available() = " + str(data_available))
        return data_available

    def get_total_samples(self, debug=False):
        if debug:
            print("DAQ: get_total_samples() = " + str(self.prev_count))
        return self.prev_count

    # Return an array of size chunk_size of all new data available since the last call to read()
    # Return None if less than chunk_size data is available
    # Raise error if buffer overrun has occurred
    def read(self, chunk_size, debug=False):
        if debug:
            print("DAQ: read(chunk_size=" + str(chunk_size) + ")")

        if self.data_available() < chunk_size:
            return None

        if self.data_available() > self.total_buffer_size:
            raise RuntimeError('Error: Buffer overrun')

        # Allocate an array of doubles for temporary data storage
        chunk_data = (c_double * chunk_size)()

        # Do segmented copy if the data wraps around the end of the buffer 
        if self.prev_index + chunk_size > self.total_buffer_size - 1:
            first_chunk_size = self.total_buffer_size - self.prev_index
            second_chunk_size = chunk_size - first_chunk_size

            chunk_data[:first_chunk_size] = self.data_buffer[self.prev_index:]
            chunk_data[first_chunk_size:] = self.data_buffer[:second_chunk_size]
        else:
            chunk_data = self.data_buffer[self.prev_index:(self.prev_index+chunk_size)]

        # Check for a buffer overrun just after copying the data from the buffer
        # This will ensure that the data was not overwritten in the buffer before the copy was completed
        _, transfer_status = self.ai_device.get_scan_status()
        if transfer_status.current_total_count - self.prev_count > self.total_buffer_size:
            raise RuntimeError('Error: Buffer overrun')
        
        self.prev_count += chunk_size
        self.prev_index += chunk_size
        self.prev_index %= self.total_buffer_size   # Wrap prev_index to the size of the buffer

        #print('\tSamples written to disk:', self.prev_count, end='', flush=True)

        return chunk_data


# Create DAQ device
daq = DAQ()

# GPIO pin assignments
LED1 = 16
LED2 = 18
button = 22
button_event = ButtonEvent(button, active_low=True)

error_message = ''
status_message = ''


##### Flask helper functions #####

def get_value_from_range_index(index):
    if index == 0:
        return 10
    elif index == 1:
        return 5
    elif index == 2:
        return 2
    elif index == 3:
        return 1
    return 10

def get_range_index_from_value(value):
    if value == '1':
        return 3
    elif value == '2':
        return 2
    elif value == '5':
        return 1
    elif value == '10':
        return 0
    return 0

def get_value_from_mode(mode):
    if mode == AiInputMode.SINGLE_ENDED:
        return 'single'
    elif mode == AiInputMode.DIFFERENTIAL:
        return 'differential'
    return 'single'

def get_mode_from_value(value):
    if value == 'single':
        return AiInputMode.SINGLE_ENDED
    elif value == 'differential':
        return AiInputMode.DIFFERENTIAL
    return AiInputMode.SINGLE_ENDED


##### Flask functions #####

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.disabled = True

data_rate_changed = False

@app.route('/index')
def index():
    global data_rate_changed
    data_rate_changed = True
    scan_running = daq.scan_running()
    return render_template('index.html', start_disabled=scan_running, stop_disabled=not scan_running)

@app.route('/start', methods=["POST"])
def start():
    daq.set_start_pending(debug=True)
    return ("")
    #return redirect(url_for('index'))

@app.route('/stop', methods=["POST"])
def stop():
    daq.set_stop_pending(debug=True)
    return ("")
    #return redirect(url_for('index'))

@app.route('/rate', methods=["POST"])
def rate():
    global data_rate_changed
    value = request.form.get('val')
    print("New data rate value: " + str(value))
    daq.set_data_rate(int(value), debug=True)
    print("DAQ data rate set to: " + str(daq.get_data_rate()))
    data_rate_changed = True
    return ("")

@app.route('/input_range', methods=["POST"])
def input_range():
    value = request.form.get('val')
    daq.set_range_index(get_range_index_from_value(value), debug=True)
    return ("")

@app.route('/mode', methods=["POST"])
def mode():
    value = request.form.get('val')
    daq.set_input_mode(get_mode_from_value(value), debug=True)
    return ("")

@app.route('/status', methods=["GET", "POST"])
def status():
    global data_rate_changed
    scan_running = daq.scan_running()
    rate_value = int(daq.get_data_rate())
    range_value = get_value_from_range_index(daq.get_range_index())
    mode_value = get_value_from_mode(daq.get_input_mode())
    cur_status = {
        'start_disabled': scan_running,
        'stop_disabled': not scan_running,
        'rate_value': rate_value,
        'range_value': range_value,
        'mode_value': mode_value,
        'error_message': error_message,
        'status_message': status_message,
        'update_rate_value': data_rate_changed
    }
    data_rate_changed = False
    return jsonify(cur_status)

def run_flask():
    app.run(debug=True, use_reloader=False, host="0.0.0.0")


def get_write_speed(write_ascii=True, debug=False):
    if debug:
        print("Calibrating write speed")

    if write_ascii:
        file_arguments = 'w'
    else:
        file_arguments = 'wb'
    f = open('write_test.bin', file_arguments)

    # Create test array of floats
    test_data = create_float_buffer(1, 10000)
    for i in range(len(test_data)):
        test_data[i] = 1.0 * i

    start_time = datetime.datetime.now().timestamp()

    for i in range(100):
        if write_ascii:
            # Write values to file in ASCII
            for i in range(len(test_data)):
                #f.write(str(new_data[i]) + ',\t')
                f.write("{:16.12f},\t".format(test_data[i]))
        else:
            # Write values to file in bytes
            s = struct.pack('d' * len(test_data), *test_data)
            f.write(s)

        # Force write to disk
        f.flush()
        os.fsync(f.fileno()) 

    end_time = datetime.datetime.now().timestamp()
    total_time = end_time - start_time
    write_speed = 1000000 / total_time  # Samples/second

    f.close()
    os.remove('write_test.bin')
    
    print("Write speed:", write_speed, "samples per second")

    return write_speed
    

if __name__ == '__main__':
    buffer_size_seconds = 10    # Size of the data buffer to create, in seconds
    disk_refresh_seconds = 1    # Time in seconds between each write to disk (should be less than 1/2 buffer_size_seconds)
    sleep_time_seconds = 0.1    # Time to sleep between writing chunks (should be less than disk_refresh_seconds)
    file_name = 'a_in_scan_file_data.csv'
    file_ascii = True

    #daq.set_num_channels(6)
    #daq.set_data_rate(10000)
    #daq.set_range_index(1)
    #daq.set_input_mode(AiInputMode.DIFFERENTIAL)

    write_speed = get_write_speed(write_ascii=file_ascii, debug=True)
    max_write_speed = write_speed * 0.5

    # Start flask in background thread
    t1 = threading.Thread(target=run_flask)
    t1.daemon = True
    t1.start()

    time.sleep(1)

    while True:
        try:
            if daq.get_start_pending():
                daq.connect(int(max_write_speed), debug=True)

                # Create data buffer
                channel_buffer_size = buffer_size_seconds * daq.get_data_rate()
                data_buffer = create_float_buffer(daq.get_num_channels(), channel_buffer_size)

                total_buffer_size = channel_buffer_size * daq.get_num_channels()
                write_chunk_size = int((total_buffer_size * disk_refresh_seconds) / buffer_size_seconds)

                f = open(file_name, 'w')

                # Write file header
                for channel_num in range(0, daq.get_num_channels()):
                    f.write('Channel ' + str(channel_num) + '\t\t')
                f.write('\n')

                write_channel_num = 0

                daq.start_scan(data_buffer, buffer_size_seconds)
                
                # Update rate on webpage in case in changed during startup
                data_rate_changed = True

                while daq.scan_running():
                    if daq.data_available() > write_chunk_size:
                        new_data = daq.read(write_chunk_size, debug=True)
                        
                        if file_ascii:
                            # Write values to file in ASCII
                            for i in range(write_chunk_size):
                                #f.write(str(new_data[i]) + ',\t')
                                f.write("{:16.12f},\t".format(new_data[i]))
                                write_channel_num += 1
                                if write_channel_num == daq.get_num_channels():
                                    write_channel_num = 0
                                    f.write(u'\n')
                        else:
                            # Write values to file in bytes
                            s = struct.pack('d' * write_chunk_size, *new_data)
                            f.write(s)
                            f.write(bytes('\n', 'utf-8'))

                        # Force write to disk
                        f.flush()
                        os.fsync(f.fileno()) 

                        status_message = "Samples written to disk: " + str(daq.get_total_samples())

                    if daq.get_stop_pending():
                        break
                    
                    time.sleep(sleep_time_seconds)

                daq.stop_scan(debug=True)
                daq.disconnect(debug=True)

                GPIO.cleanup()
                f.close()

        except Exception as e:
            formatted_error = traceback.format_exc()
            error_message = str(datetime.datetime.now()) + ':\n' + formatted_error
            print(error_message)
        
        time.sleep(1)
