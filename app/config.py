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
import logging
import logging.config
from pythonjsonlogger import jsonlogger
from dotenv import load_dotenv

class Config:
    load_dotenv()
    
    # Operation type
    OPERATION = os.getenv('OPERATION', "BACKUP").upper()
    if OPERATION not in ['BACKUP', 'RESTORE', 'VERIFY', 'BACKUP-PARITY', 'RESTORE-PARITY']:
        OPERATION = 'BACKUP'

    # Backup file name
    BACKUP_FILE_NAME = os.getenv('BACKUP_FILE_NAME', "backup")

    # Compression level
    COMPRESSION = os.getenv('COMPRESSION', "MEDIUM").upper()
    if COMPRESSION not in ['NONE', 'LOW', 'MEDIUM', 'HIGH']:
        COMPRESSION = 'MEDIUM'

    # Encryption
    ENCRYPTION = os.getenv('ENCRYPTION', "NO").upper()
    if ENCRYPTION not in ['YES', 'NO']:
        ENCRYPTION = 'NO'
    
    # Encryption key
    ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', "super-secure.pass_123")
    
    # Logger config
    LOG_OUTPUT = os.getenv('LOG_OUTPUT', 'console').split(',')
    LOG_OUTPUT = [output.strip() for output in LOG_OUTPUT if output.strip() in ['console', 'file', 'json_file']]
    if not LOG_OUTPUT:
        LOG_OUTPUT = ['console']
    
    LOGS_PATH = os.getenv('LOGS_PATH', '/app/logs')
    
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()
    if LOG_LEVEL not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        LOG_LEVEL = 'INFO'
    
    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            },
            "json": {
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
                "class": "pythonjsonlogger.jsonlogger.JsonFormatter"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": LOG_LEVEL,
                "formatter": "standard",
                "stream": "ext://sys.stdout"
            },
            "file": {
                "class": "logging.FileHandler",
                "level": LOG_LEVEL,
                "formatter": "standard",
                "filename": os.path.join(LOGS_PATH, "logfile.log"),
                "mode": "a"
            },
            "json_file": {
                "class": "logging.FileHandler",
                "level": LOG_LEVEL,
                "formatter": "json",
                "filename": os.path.join(LOGS_PATH, "logfile.json"),
                "mode": "a"
            }
        },
        "loggers": {
            "": {
                "handlers": LOG_OUTPUT,
                "level": LOG_LEVEL,
                "propagate": True
            }
        }
    }
    
    logger = logging.getLogger()
    logging.config.dictConfig(LOGGING_CONFIG)
    
    # Base directory
    BASEDIR = os.path.abspath(os.path.dirname(__file__))
    
    
    def boot_info(self):
        
        self.logger.info(f"-= Starting DOCKER-VOLUME-MANAGER =-")
        self.logger.info(f"\tLOG_LEVEL: {self.LOG_LEVEL}")
        self.logger.info(f"\tLOG_OUTPUT: {self.LOG_OUTPUT}")
    
        if self.LOG_LEVEL == 'DEBUG':
            self.logger.debug(f"\tOPERATION: {self.OPERATION}")
            self.logger.debug(f"\tBACKUP_FILE_NAME: {self.BACKUP_FILE_NAME}")
            self.logger.debug(f"\tCOMPRESSION: {self.COMPRESSION}")
            self.logger.debug(f"\tENCRYPTION: {self.ENCRYPTION}")
            self.logger.debug(f"\tENCRYPTION_KEY: {self.ENCRYPTION_KEY}")
            self.logger.debug(f"\tLOGS_PATH: {self.LOGS_PATH}")
            self.logger.debug(f"\tLOGGING_CONFIG: {self.LOGGING_CONFIG}")

        if self.OPERATION == 'BACKUP' or self.OPERATION == 'BACKUP-PARITY':
            self.logger.info(f"Performing operation: {self.OPERATION} using compression level: {self.COMPRESSION} (encryption: {self.ENCRYPTION})")

        if self.OPERATION == 'RESTORE' or self.OPERATION == 'RESTORE-PARITY' or self.OPERATION == 'VERIFY':
            self.logger.info(f"Performing operation: {self.OPERATION} (encryption: {self.ENCRYPTION})")
