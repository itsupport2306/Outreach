import os
from dotenv import load_dotenv

# Load local development variables without overriding deployment environment.
load_dotenv()

# Import the messaging service components
from .messaging import get_messaging_service, messaging_service

# For backward compatibility
def init_messaging_service(db):
    """Initialize the messaging service with a database session.
    
    Args:
        db: SQLAlchemy database session
        
    Returns:
        The initialized messaging service instance
    """
    return get_messaging_service(db=db)

# This will be the main entry point for other modules
__all__ = ['messaging_service', 'get_messaging_service']
