import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Union, Any
from datetime import datetime
from modules.claude_sdk_compat import ClaudeSDKClient


logger = logging.getLogger(__name__)



@dataclass
class UserSession:
    user_id: Union[int, str]
    chat_id: Union[int, str]
    is_executing: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    # Map of session_id to ClaudeSDKClient instance
    claude_clients: Dict[str, ClaudeSDKClient] = field(default_factory=dict)
    # Map of session_id to message receiver task
    receiver_tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    # Map of session_id to boolean indicating if session is waiting for result
    session_active: Dict[str, bool] = field(default_factory=dict)
    
    
    async def cleanup_clients(self):
        """Cleanup all Claude SDK clients and receiver tasks"""
        # Cancel all receiver tasks first
        for session_id, task in self.receiver_tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info(f"Cancelled receiver task for session {session_id}")
        
        # Then disconnect clients
        for session_id, client in self.claude_clients.items():
            try:
                await client.disconnect()
                logger.info(f"Disconnected Claude client for session {session_id}")
            except Exception as e:
                logger.error(f"Error disconnecting Claude client for session {session_id}: {e}")
        
        self.receiver_tasks.clear()
    
    def get_status(self) -> str:
        """Get session status summary"""
        status = f"📊 Session Status\n"
        status += f"━━━━━━━━━━━━━━━━\n"
        status += f"User ID: {self.user_id}\n"
        status += f"Active sessions: {len(self.claude_clients)}\n"
        status += f"Status: {'🟢 Connected' if self.claude_clients else '⭕ No active session'}\n"
        status += f"Last activity: {self.last_activity.strftime('%Y-%m-%d %H:%M:%S')}"
        
        if self.claude_clients:
            status += "\n\n🔗 Active Claude sessions:"
            for session_id in self.claude_clients:
                status += f"\n• {session_id}"
        else:
            status += "\n\n💬 Send a message to start a conversation"
        
        return status


class SessionManager:
    def __init__(self):
        self.sessions: Dict[Union[int, str], UserSession] = {}
        self._lock = asyncio.Lock()
    
    async def get_or_create_session(self, user_id: Union[int, str], chat_id: Union[int, str]) -> UserSession:
        """Get existing session or create new one"""
        async with self._lock:
            if user_id not in self.sessions:
                self.sessions[user_id] = UserSession(user_id=user_id, chat_id=chat_id)
                logger.info(f"Created new session for user {user_id}")
            
            return self.sessions[user_id]
    
    
    async def clear_session(self, user_id: Union[int, str]) -> str:
        """Clear user's session and disconnect all Claude clients"""
        if user_id not in self.sessions:
            return "No active session found."
        
        session = self.sessions[user_id]
        
        # Cleanup all Claude SDK clients and receiver tasks
        client_count = len(session.claude_clients)
        await session.cleanup_clients()
        session.claude_clients.clear()
        session.receiver_tasks.clear()
        
        return f"Cleared {client_count} active Claude session(s)."
    
    async def get_status(self, user_id: Union[int, str]) -> str:
        """Get user's session status"""
        if user_id not in self.sessions:
            return "No active session. Send a message to start."
        
        session = self.sessions[user_id]
        return session.get_status()
    
    async def set_executing(self, user_id: Union[int, str], is_executing: bool):
        """Set execution status for user session"""
        if user_id in self.sessions:
            async with self._lock:
                self.sessions[user_id].is_executing = is_executing
                self.sessions[user_id].last_activity = datetime.now()
    
    async def is_executing(self, user_id: Union[int, str]) -> bool:
        """Check if user has an active execution"""
        if user_id not in self.sessions:
            return False
        return self.sessions[user_id].is_executing
