#!/usr/bin/env python3
"""
sepian_plugin.py - Base plugin class for Sepian
"""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class SepianPlugin(ABC):
    """Base class for all Sepian plugins"""
    
    def __init__(self):
        self.name = self.__class__.__name__
        self.enabled = True
        self.config = {}
        self.status_callback = None
    
    @abstractmethod
    def get_description(self) -> str:
        """Return plugin description"""
        pass
    
    @abstractmethod
    def get_commands(self) -> List[str]:
        """Return list of available commands"""
        pass
    
    def get_default_config(self) -> Dict[str, Any]:
        """Return default configuration"""
        return {}
    
    def set_config(self, config: Dict[str, Any]):
        """Apply configuration"""
        self.config = config
        self.on_config_update()
    
    def on_config_update(self):
        """Called when config is updated - override if needed"""
        pass
    
    @abstractmethod
    def execute(self, command: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a command - returns dict with 'ok' key
        Example: {'ok': True, 'result': 'something'}
        """
        pass
    
    def handle_voice_command(self, text: str) -> Optional[str]:
        """
        Try to handle a voice command
        Returns response string if handled, None otherwise
        """
        return None
    
    def notify_status(self, message: str):
        """Send status update if callback is registered"""
        if self.status_callback:
            self.status_callback(self.name, message)
    
    def get_info(self) -> Dict[str, Any]:
        """Return plugin information"""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "description": self.get_description(),
            "commands": self.get_commands(),
            "config": self.config
        }
