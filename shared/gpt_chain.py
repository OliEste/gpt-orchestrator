"""
GPT-to-GPT chain execution module.
Handles sequential execution of multiple GPTs where each GPT's output
becomes the next GPT's input.
"""
from typing import List, Dict, Optional
from .responses_api import ResponsesAPIClient


class GPTChain:
    """Manages sequential execution of GPT agents."""
    
    def __init__(self, api_client: Optional[ResponsesAPIClient] = None):
        """
        Initialize the GPT chain.
        
        Args:
            api_client: ResponsesAPIClient instance. If None, creates a new one.
        """
        self.api_client = api_client or ResponsesAPIClient()
        self.execution_log: List[Dict] = []
    
    def execute_chain(
        self,
        initial_input: str,
        gpt_configs: List[Dict],
        return_intermediate: bool = False
    ) -> Dict:
        """
        Execute a chain of GPTs sequentially.
        """
        self.execution_log = []
        current_input = initial_input
        
        chain_outputs = {}
        
        for i, gpt_config in enumerate(gpt_configs):
            gpt_name = gpt_config.get('name', f'gpt_{i+1}')
            instructions = gpt_config.get('instructions', '')
            model = gpt_config.get('model', 'gpt-4')
            temperature = gpt_config.get('temperature', 0.7)
            tools = gpt_config.get('tools', None)
            
            # Prepare input for this GPT
            if i == 0:
                user_input = current_input
            else:
                # Include previous GPT's output
                previous_gpt = gpt_configs[i-1].get('name', f'gpt_{i}')
                user_input = f"""Based on the previous step's output, continue with your task.

                Previous output from {previous_gpt}:
                {current_input}

                Now proceed with your designated task."""
            
            # Execute this GPT
            try:
                output = self.api_client.create_response(
                    system_prompt=instructions,
                    user_input=user_input,
                    model=model,
                    temperature=temperature,
                    tools=tools
                )
                
                # Log execution
                execution_entry = {
                    'step': i + 1,
                    'gpt_name': gpt_name,
                    'input': user_input,
                    'output': output,
                    'status': 'success'
                }
                self.execution_log.append(execution_entry)
                
                if return_intermediate:
                    chain_outputs[gpt_name] = output
                
                # Set output as input for next GPT
                current_input = output
                
            except Exception as e:
                execution_entry = {
                    'step': i + 1,
                    'gpt_name': gpt_name,
                    'input': user_input,
                    'output': None,
                    'status': 'error',
                    'error': str(e)
                }
                self.execution_log.append(execution_entry)
                raise Exception(f"Error in {gpt_name}: {str(e)}")
        
        result = {
            'final_output': current_input,
            'execution_log': self.execution_log
        }
        
        if return_intermediate:
            result['chain_outputs'] = chain_outputs
        
        return result

