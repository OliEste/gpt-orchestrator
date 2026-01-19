"""
Logging configuration for the Constructability orchestrator.
Sets up file-based logging with rotation for IIS deployment.
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime

def setup_logging(log_dir: str = None, log_level: int = logging.INFO):
    """
    Set up logging configuration with file and console handlers.
    
    Args:
        log_dir: Directory for log files. Defaults to 'logs' in the project root.
        log_level: Logging level (logging.DEBUG, logging.INFO, etc.)
    """
    # Determine log directory
    if log_dir is None:
        # Get the project root (parent of constructability directory)
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        log_dir = os.path.join(project_root, 'logs')
    
    # Create logs directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)
    
    # Create log filename with timestamp
    log_filename = os.path.join(log_dir, f'constructability_{datetime.now().strftime("%Y%m%d")}.log')
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(log_level)
    
    # Remove existing handlers to avoid duplicates
    logger.handlers = []
    
    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    simple_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # File handler with rotation (10MB per file, keep 5 backups)
    file_handler = RotatingFileHandler(
        log_filename,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(detailed_formatter)
    logger.addHandler(file_handler)
    
    # Console handler (for local development)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    console_handler.setFormatter(simple_formatter)
    logger.addHandler(console_handler)
    
    # Log startup message
    logger.info("=" * 80)
    logger.info("Logging initialized")
    logger.info(f"Log file: {log_filename}")
    logger.info(f"Log level: {logging.getLevelName(log_level)}")
    logger.info("=" * 80)
    
    return logger

def get_logger(name: str):
    """
    Get a logger instance for a specific module.
    
    Args:
        name: Logger name (typically __name__)
    
    Returns:
        Logger instance
    """
    return logging.getLogger(name)