import os
import sys

# Add parent directory to path for shared imports
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Add current directory to path for local imports
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from orchestrator import app
from shared.config import SharedConfig
from logging_config import setup_logging, get_logger
from waitress import serve

# Setup logging
setup_logging()
logger = get_logger(__name__)

if __name__ == '__main__':
    # HttpPlatformHandler sets HTTP_PLATFORM_PORT environment variable
    port = os.environ.get('HTTP_PLATFORM_PORT') or os.environ.get('PORT', '5000')
    
    # Convert to int if it's a string
    try:
        port = int(port)
    except (ValueError, TypeError):
        logger.warning(f"Invalid port value: {port}, defaulting to 5000")
        port = 5000
    
    logger.info("="*80)
    logger.info("Starting Constructability Orchestrator via HttpPlatformHandler (Production)")
    logger.info(f"Port: {port}")
    logger.info(f"Host: 127.0.0.1 (IIS handles external connections)")
    logger.info(f"Server: Waitress (Production WSGI)")
    logger.info(f"Egnyte Base URL: {SharedConfig.EGNYTE_BASE_URL}")
    logger.info("="*80)
    
    # Validate configuration
    try:
        SharedConfig.validate()
    except ValueError as e:
        logger.error(f"Configuration validation failed: {e}")
        raise
    
    # Run with Waitress production server
    # Bind to 127.0.0.1 because IIS HttpPlatformHandler handles external connections
    # IIS will forward requests to this local port
    serve(
        app,
        host='127.0.0.1',
        port=port,
        threads=4,  # Number of worker threads
        channel_timeout=1800,  # Timeout for request handling
        cleanup_interval=30,  # Cleanup interval in seconds
        asyncore_use_poll=True  
    )
