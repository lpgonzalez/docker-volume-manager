"""
Copyright (c) 2025 Lisardo Prieto <lisardo.prieto@datos101.com> and contributors.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NON INFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import os
import time
from threading import Thread
from config import Config
from operations.docker_volume_manager import Docker_Volume_Manager

# Temporary in-memory file to store the app status
STATUS_FILE = '/dev/shm/app_status.txt'

def update_status(status):
    with open(STATUS_FILE, 'w') as f:
        f.write(status)

def run_app():
    c = Config()
    logger = c.logger

    c.boot_info()

    try:
        update_status('healthy')
        dvm = Docker_Volume_Manager(c)
        dvm.run()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        update_status('unhealthy')

if __name__ == '__main__':
    c = Config()
    logger = c.logger
    
    update_status('starting')
    scheduler_thread = Thread(target=run_app)
    scheduler_thread.start()

    while scheduler_thread.is_alive():
        time.sleep(5)
    update_status('unhealthy')
