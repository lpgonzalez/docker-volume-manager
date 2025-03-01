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

import time
from config import Config

class Docker_Volume_Manager:
    def __init__(self, config: Config):
        self.config = config
        self.logger = config.logger

    def run(self):
        if self.config.OPERATION == 'BACKUP':
            self.backup()
        elif self.config.OPERATION == 'RESTORE':
            self.restore()
        elif self.config.OPERATION == 'VERIFY':
            self.verify()
        elif self.config.OPERATION == 'BACKUP-PARITY':
            self.backup_parity()
        elif self.config.OPERATION == 'RESTORE-PARITY':
            self.restore_parity()
        else:
            self.logger.error("Invalid operation")



    def backup(self):
        self.logger.info("Backing up Docker volume")
        # Code to backup Docker volumes
        time.sleep(60)
    
    def restore(self):
        self.logger.info("Restoring Docker volume")
        # Code to restore Docker volumes
        time.sleep(60)
    
    def verify(self):
        self.logger.info("Verifying Docker volume backup")
        # Code to verify Docker volumes
        time.sleep(60)
    
    def backup_parity(self):
        self.logger.info("Backing up Docker volume with parity")
        # Code to backup Docker volumes with parity
        time.sleep(60)
    
    def restore_parity(self):
        self.logger.info("Restoring Docker volume with parity")
        # Code to restore Docker volumes with parity
        time.sleep(60)