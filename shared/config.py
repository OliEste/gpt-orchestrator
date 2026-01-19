"""
Shared configuration for orchestrators.
Contains common settings and utilities.
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class SharedConfig:
    """Shared configuration settings."""
    
    # OpenAI API settings
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    DEFAULT_MODEL = os.getenv('DEFAULT_MODEL', 'gpt-4')
    DEFAULT_TEMPERATURE = float(os.getenv('DEFAULT_TEMPERATURE', '0.7'))
    
    # Server settings
    PORT = int(os.getenv('PORT', '5000'))
    DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'
    
    # Egnyte OAuth settings (for OAuth proxy)
    EGNYTE_CLIENT_ID = os.getenv('EGNYTE_CLIENT_ID')
    EGNYTE_CLIENT_SECRET = os.getenv('EGNYTE_CLIENT_SECRET')
    EGNYTE_AUTHORIZATION_URL = os.getenv('EGNYTE_AUTHORIZATION_URL', 'https://coastalconstruction.egnyte.com/puboauth/v1/authorize')
    EGNYTE_TOKEN_URL = os.getenv('EGNYTE_TOKEN_URL', 'https://coastalconstruction.egnyte.com/puboauth/v1/token')
    EGNYTE_BASE_URL = os.getenv('EGNYTE_BASE_URL', 'https://coastalconstruction.egnyte.com')
    
    @classmethod
    def validate(cls):
        """Validate that required configuration is present."""
        if not cls.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required in environment variables")
        return True

