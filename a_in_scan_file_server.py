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

# GPIO pin assignments
LED1 = 16
LED2 = 18
button = 22
button_event = ButtonEvent(button, active_low=True)

# Global parameters (to be modified by run_log or server)
range_index = 1 # 0 = 10V, 1 = 5V, 2 = 2V, 3 = 1V
input_mode = AiInputMode.DIFFERENTIAL
low_channel = 0
high_channel = 2
data_rate = 80000
max_write_speed = 200000    # Bytes/second USB write speed: total throughput should not exceed this value
file_name = 'a_in_scan_file_data.csv'
file_binary = False

log_start_queued = False
log_stop_queued = False
log_running = False
error_message = ''
status_message = ''

def run_log():
    global range_index
    global input_mode
    global low_channel
    global high_channel
    global data_rate
    global file_name
    global file_binary
    global log_start_queued
    global log_stop_queued
    global log_running
    global error_message
    global status_message

    # Function parameters
    device_num = 0
    interface_type = InterfaceType.ANY
    scan_options = ScanOption.CONTINUOUS
    flags = AInScanFlag.DEFAULT
    buffer_size_seconds = 10    # The size of the buffer to create, in seconds
    disk_refresh_seconds = 1    # Time in seconds between each write to disk (should be less than 1/2 buffer_size_seconds)
    
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(LED1, GPIO.OUT)                        
    GPIO.setup(LED2, GPIO.OUT)                    
    GPIO.setup(button, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.output(LED1, 0)
    GPIO.output(LED2, 0)
 
    daq_device = None
    ai_device = None
    status = ScanStatus.IDLE

    log_running = False

    # Wait for user input
    print('\nPress Space or Start Button or connect to server to start data collection\n')
    key_pressed = False
    button_pressed = False
    log_start_queued = False
    while not (key_pressed or button_pressed or log_start_queued):
        button_event.clear_pressed()
        button_pressed = button_event.is_pressed()
        #key_pressed = keyboard.is_pressed('space')
    
    log_stop_queued = False
    log_running = True
    error_message = ''
    status_message = ''

    # Get descriptors for all of the available DAQ devices.
    devices = get_daq_device_inventory(interface_type)
    number_of_devices = len(devices)
    if number_of_devices == 0:
        raise RuntimeError('Error: No DAQ devices found')
    device_num = min(device_num, number_of_devices-1)
    
    print('Found', number_of_devices, 'DAQ device(s):')
    for i in range(number_of_devices):
        print('  [', i, '] ', devices[i].product_name, ' (',
              devices[i].unique_id, ')', sep='')
    print("\nUsing DAQ device", device_num)

    # Indicate system ready
    GPIO.output(LED2, 1)

    # Create the DAQ device from the descriptor at the specified index.
    daq_device = DaqDevice(devices[0])

    # Get the AiDevice object and verify that it is valid.
    ai_device = daq_device.get_ai_device()
    if ai_device is None:
        raise RuntimeError('Error: The DAQ device does not support analog '
                           'input')

    # Verify the specified device supports hardware pacing for analog input.
    ai_info = ai_device.get_info()
    if not ai_info.has_pacer():
        raise RuntimeError('\nError: The specified DAQ device does not '
                           'support hardware paced analog input')

    # Establish a connection to the DAQ device.
    descriptor = daq_device.get_descriptor()
    print('\nConnecting to', descriptor.dev_string, '- please wait...')
    daq_device.connect(connection_code=0)

    # Get the number of channels and validate the high channel number.
    number_of_channels = ai_info.get_num_chans_by_mode(input_mode)
    if high_channel >= number_of_channels:
        high_channel = number_of_channels - 1
    channel_count = high_channel - low_channel + 1

    # Get a list of supported ranges and validate the range index.
    ranges = ai_info.get_ranges(input_mode)
    if range_index >= len(ranges):
        range_index = len(ranges) - 1

    min_rate = ai_info.get_min_scan_rate()
    max_rate = ai_info.get_max_scan_rate()
    max_throughput = ai_info.get_max_throughput()
    max_throughput_rate = max_throughput / channel_count
    max_write_speed_rate = (max_write_speed * disk_refresh_seconds) / channel_count
    data_rate = max(data_rate, min_rate)
    data_rate = min(data_rate, max_rate, max_throughput_rate, max_write_speed_rate)
    data_rate = int(data_rate)

    # Create a circular buffer that can hold buffer_size_seconds worth of
    # data, or at least 10 samples
    samples_per_channel = max(data_rate * buffer_size_seconds, 10)
    total_buffer_size = samples_per_channel * channel_count
    data = create_float_buffer(channel_count, samples_per_channel)

    # When handling the buffer, we will read only part of the buffer at a time
    write_chunk_size = int((total_buffer_size * disk_refresh_seconds) / buffer_size_seconds)

    # Allocate an array of doubles temporary storage of the data
    write_chunk_array = (c_double * write_chunk_size)()

    print('\n', descriptor.dev_string, ' ready', sep='')
    print('    Function demonstrated: ai_device.a_in_scan()')
    print('    Channels: ', low_channel, '-', high_channel)
    print('    Input mode: ', input_mode.name)
    print('    Range: ', ranges[range_index].name)
    print('    Rate: ', data_rate, 'Hz')
    print('    Scan options:', display_scan_options(scan_options))
    print()

    print("Total buffer size:", total_buffer_size)
    print("Disk write chunk size:", write_chunk_size)
    print('Data collection started. Press Space or Start Button to end\n')
    ct = datetime.datetime.now()
    print('Time started:', ct)
    
    # Create file
    f = open(file_name, 'w')

    # Write file header
    for chan_num in range(low_channel, high_channel+1):
        f.write('Channel ' + str(chan_num) + '\t\t')
    f.write('\n')

    # Start the acquisition
    actual_rate = ai_device.a_in_scan(low_channel, high_channel, input_mode,
                               ranges[range_index], samples_per_channel,
                               data_rate, scan_options, flags, data)
    prev_count = 0
    prev_index = 0
    write_ch_num = low_channel

    # Wait for the scan to start fully
    status = ScanStatus.IDLE
    while status == ScanStatus.IDLE:
        status, _ = ai_device.get_scan_status()

    # Indicate scan started
    GPIO.output(LED1, 1)

    while status != ScanStatus.IDLE:
        # Get the status of the background operation
        status, transfer_status = ai_device.get_scan_status()

        new_data_count = transfer_status.current_total_count - prev_count

        # Check for a buffer overrun before copying the data, so that 
        # no attempts are made to copy more than a full buffer of data
        if new_data_count > total_buffer_size:
            # Print an error and stop writing
            print('\nERROR: A buffer overrun occurred')
            print("Total samples:", transfer_status.current_total_count)
            print("Total samples written to disk:", prev_count)
            break

        # Check if a chunk is available
        if new_data_count > write_chunk_size:
            print('\rNew samples available:', new_data_count, ' '*10, end='', flush=True)
            
            # Copy the current data to a new array

            # Check if the data wraps around the end of the buffer 
            # Multiple copy operations will be required
            if prev_index + write_chunk_size > total_buffer_size - 1:
                first_chunk_size = total_buffer_size - prev_index
                second_chunk_size = (write_chunk_size - first_chunk_size)

                # Copy the first chunk of data to the write_chunk_array
                write_chunk_array[:first_chunk_size] = data[prev_index:]

                # Copy the second chunk of data to the write_chunk_array
                write_chunk_array[first_chunk_size:] = data[:second_chunk_size]
            else:
                # Copy the data to the write_chunk_array
                write_chunk_array = data[prev_index:(prev_index+write_chunk_size)]

            # Check for a buffer overrun just after copying the data
            # from the UL buffer. This will ensure that the data was
            # not overwritten in the UL buffer before the copy was
            # completed. This should be done before writing to the
            # file, so that corrupt data does not end up in it.
            status, transfer_status = ai_device.get_scan_status()
            if transfer_status.current_total_count - prev_count > total_buffer_size:
                # Print an error and stop writing
                print('\nERROR: A buffer overrun occurred')
                print("Total samples:", transfer_status.current_total_count)
                print("Total samples written to disk:", prev_count)
                break

            # Write values to file in ASCII
            for i in range(write_chunk_size):
                #f.write(str(write_chunk_array[i]) + ',\t')
                f.write("{:16.12f},\t".format(write_chunk_array[i]))
                write_ch_num += 1
                if write_ch_num == high_channel + 1:
                    write_ch_num = low_channel
                    f.write(u'\n')

            # Write values to file in bytes
            #s = struct.pack('d' * write_chunk_size, *write_chunk_array)
            #f.write(s)
            #f.write(bytes('\n', 'utf-8'))

            f.flush()
            os.fsync(f.fileno())    # Force write to disk
            wrote_chunk = True
        else:
            wrote_chunk = False

        if wrote_chunk:
            # Increment prev_count by the chunk size
            prev_count += write_chunk_size
            # Increment prev_index by the chunk size
            prev_index += write_chunk_size
            # Wrap prev_index to the size of the UL buffer
            prev_index %= total_buffer_size

            if log_stop_queued:
                break

            print('\tSamples written to disk:', prev_count, end='', flush=True)
            status_message = 'Samples written to disk: ' + str(prev_count)
        else:
            # Wait time should be no more than disk_refresh_seconds/2 to avoid buffer overflow
            time.sleep(0.05)

        # Check for user input
        button_event.clear_pressed()
        #if keyboard.is_pressed('space') or button_event.is_pressed():
        if button_event.is_pressed():
            log_stop_queued = True

    GPIO.cleanup()

    f.close()

    print('\nDone')
    ct = datetime.datetime.now()
    print('Time ended:', ct)

    if daq_device:
        # Stop the acquisition if it is still running.
        if status == ScanStatus.RUNNING:
            ai_device.scan_stop()
        if daq_device.is_connected():
            daq_device.disconnect()
        daq_device.release()

    log_running = False

def display_scan_options(bit_mask):
    """Create a displays string for all scan options."""
    options = []
    if bit_mask == ScanOption.DEFAULTIO:
        options.append(ScanOption.DEFAULTIO.name)
    for option in ScanOption:
        if option & bit_mask:
            options.append(option.name)
    return ', '.join(options)

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

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.disabled = True

@app.route('/index')
def index():
    return render_template('index.html', start_disabled=log_running, stop_disabled=not log_running)

@app.route('/start', methods=["POST"])
def start():
    global log_start_queued
    log_start_queued = True
    print ("Log Start Queued")
    return ("")
    #return redirect(url_for('index'))

@app.route('/stop', methods=["POST"])
def stop():
    global log_stop_queued
    log_stop_queued = True
    print ("Log Stop Queued")
    return ("")
    #return redirect(url_for('index'))

@app.route('/rate', methods=["POST"])
def rate():
    global data_rate
    value = request.form.get('val')
    data_rate = int(value)
    print ("Rate Change:", value)
    return ("")

@app.route('/input_range', methods=["POST"])
def input_range():
    global range_index
    value = request.form.get('val')
    range_index = get_range_index_from_value(value)
    print ("Input Range Change:", value)
    return ("")

@app.route('/mode', methods=["POST"])
def mode():
    global input_mode
    value = request.form.get('val')
    input_mode = get_mode_from_value(value)
    print ("Mode Change:", value)
    return ("")

@app.route('/status', methods=["GET", "POST"])
def status():
    rate_value = data_rate
    range_value = get_value_from_range_index(range_index)
    mode_value = get_value_from_mode(input_mode)
    cur_status = {
        'start_disabled': log_running,
        'stop_disabled': not log_running,
        'rate_value': rate_value,
        'range_value': range_value,
        'mode_value': mode_value,
        'error_message': error_message,
        'status_message': status_message
    }
    return jsonify(cur_status)

def run_flask():
    app.run(debug=True, use_reloader=False, host="0.0.0.0")

if __name__ == '__main__':
    t1 = threading.Thread(target=run_flask)
    t1.daemon = True
    t1.start()

    time.sleep(1)

    while True:
        try:
            run_log()
        except Exception as e:
            error_message = str(datetime.datetime.now()) + ': ' +  str(e)
        time.sleep(1)
