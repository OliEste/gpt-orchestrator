"""
OpenAI Responses API wrapper for GPT-to-GPT communication.
This module provides a shared interface for interacting with OpenAI's Responses API.
"""
import os
from openai import OpenAI
from typing import List, Dict, Optional, Callable, Any


class ResponsesAPIClient:
    """Client for OpenAI Responses API with stateful conversation management."""
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize the Responses API client.
        
        Args:
            api_key: OpenAI API key. If None, reads from OPENAI_API_KEY env var.
        """
        self.api_key = api_key or os.getenv('OPENAI_API_KEY')
        if not self.api_key:
            raise ValueError("OpenAI API key is required. Set OPENAI_API_KEY environment variable.")
        
        self.client = OpenAI(api_key=self.api_key)
    
    def create_response(
        self,
        system_prompt: str,
        user_input: Any,
        model: str = "gpt-5.1",
        temperature: float = 0.7,
        tools: Optional[List[Dict]] = None,
        tool_executor: Optional[Callable[[str, Dict[str, Any]], Any]] = None,
        max_tool_iterations: int = 10,
        **kwargs
    ) -> str:
        """
        Create a response using the Responses API with support for function calling and vision.
        
        Args:
            system_prompt: System message defining the GPT's role/instructions
            user_input: User's input message (can be str or list with text/images for vision)
            model: Model to use (default: gpt-5.1)
            temperature: Sampling temperature (default: 0.7)
            tools: Optional list of function/tool definitions
            tool_executor: Optional function to execute tool calls. Signature: (function_name, arguments_dict) -> result
            max_tool_iterations: Maximum number of tool call iterations (default: 10)
            **kwargs: Additional parameters for the API call
        
        Returns:
            The response content as a string
        """
        # Handle vision input (list of content items) or text input (string)
        if isinstance(user_input, list):
            # Vision input: list of content items (text and/or images)
            user_content = user_input
        else:
            # Text input: simple string
            user_content = user_input
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        
        options = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            **kwargs
        }
        
        if tools:
            options["tools"] = tools
        
        # Handle tool calls in a loop
        iteration = 0
        while iteration < max_tool_iterations:
            try:
                completion = self.client.chat.completions.create(**options)
                message = completion.choices[0].message
                
                # Add assistant message to conversation
                assistant_message = {
                    "role": "assistant",
                    "content": message.content or None
                }
                
                # Add tool calls if present
                if message.tool_calls:
                    assistant_message["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments
                            }
                        } for tc in message.tool_calls
                    ]
                
                messages.append(assistant_message)
                
                # If no tool calls, return the final response
                if not message.tool_calls:
                    return message.content or ""
                
                # Execute tool calls
                if not tool_executor:
                    raise Exception("Tool calls detected but no tool_executor provided")
                
                # Add tool results to messages
                for tool_call in message.tool_calls:
                    function_name = tool_call.function.name
                    try:
                        import json
                        arguments = json.loads(tool_call.function.arguments)
                        result = tool_executor(function_name, arguments)
                        
                        # Add tool result message
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": str(result) if not isinstance(result, str) else result
                        })
                    except Exception as e:
                        # Add error as tool result
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": f"Error executing {function_name}: {str(e)}"
                        })
                
                # Update options for next iteration
                options["messages"] = messages
                iteration += 1
                
            except Exception as e:
                if iteration == 0:
                    # First iteration failed - might not be a tool call issue
                    raise Exception(f"Error calling Responses API: {str(e)}")
                else:
                    # Tool execution failed
                    raise Exception(f"Error during tool execution: {str(e)}")
        
        raise Exception(f"Maximum tool call iterations ({max_tool_iterations}) exceeded")
    
    def create_response_with_context(
        self,
        system_prompt: str,
        conversation_history: List[Dict[str, str]],
        model: str = "gpt-5.1",
        **kwargs
    ) -> str:
        """
        Create a response with conversation history (stateful).
        
        Args:
            system_prompt: System message
            conversation_history: List of message dicts with 'role' and 'content'
            model: Model to use
            **kwargs: Additional parameters
        
        Returns:
            The response content as a string
        """
        messages = [{"role": "system", "content": system_prompt}] + conversation_history
        
        options = {
            "model": model,
            "messages": messages,
            **kwargs
        }
        
        try:
            completion = self.client.chat.completions.create(**options)
            return completion.choices[0].message.content
        except Exception as e:
            raise Exception(f"Error calling Responses API with context: {str(e)}")

