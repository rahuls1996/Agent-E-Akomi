import json
import os
import tempfile
import traceback
from string import Template
import autogen  # type: ignore
import openai
#from autogen import Cache
from dotenv import load_dotenv
from ae.core.agents.browser_nav_agent import BrowserNavAgent
from ae.core.agents.high_level_planner_agent import PlannerAgent  
from ae.core.prompts import LLM_PROMPTS
from ae.utils.logger import logger
import nest_asyncio # type: ignore
from ae.core.post_process_responses import final_reply_callback_planner_agent as print_message_from_planner  # type: ignore
nest_asyncio.apply()  # type: ignore

class AutogenWrapper:
    """
    A wrapper class for interacting with the Autogen library.

    Args:
        max_chat_round (int): The maximum number of chat rounds.

    Attributes:
        number_of_rounds (int): The maximum number of chat rounds.
        agents_map (dict): A dictionary of the agents that are instantiated in this autogen instance.

    """

    def __init__(self, max_chat_round: int = 50):
        self.number_of_rounds = max_chat_round
        self.agents_map: dict[str, autogen.UserProxyAgent | autogen.AssistantAgent | autogen.ConversableAgent ] | None = None
        self.config_list: list[dict[str, str]] | None = None

    @classmethod
    async def create(cls, agents_needed: list[str] | None = None, max_chat_round: int = 50):
        """
        Create an instance of AutogenWrapper.

        Args:
            agents_needed (list[str], optional): The list of agents needed. If None, then ["user", "browser_nav_executor", "planner_agent", "browser_nav_agent"] will be used.
            max_chat_round (int, optional): The maximum number of chat rounds. Defaults to 50.

        Returns:
            AutogenWrapper: An instance of AutogenWrapper.

        """
        print(f">>> Creating AutogenWrapper with {agents_needed} and {max_chat_round} rounds.")
        if agents_needed is None:
            agents_needed = ["user", "browser_nav_executor", "planner_agent", "browser_nav_agent"]
        # Create an instance of cls
        self = cls(max_chat_round)
        load_dotenv()
        os.environ["AUTOGEN_USE_DOCKER"] = "False"

        autogen_model_name = os.getenv("AUTOGEN_MODEL_NAME")
        if not autogen_model_name:
            autogen_model_name = "gpt-4o"
            logger.warning(f"Cannot find AUTOGEN_MODEL_NAME in the environment variables, setting it to default {autogen_model_name}.")

        autogen_model_api_key = os.getenv("AUTOGEN_MODEL_API_KEY")
        if autogen_model_api_key is None:
            logger.warning("Cannot find AUTOGEN_MODEL_API_KEY in the environment variables.")
            if not os.getenv('OPENAI_API_KEY'):
                logger.error("Cannot find OPENAI_API_KEY in the environment variables.")
                raise ValueError("You need to set either AUTOGEN_MODEL_API_KEY or OPENAI_API_KEY in the .env file.")
            else:
                autogen_model_api_key = os.environ['OPENAI_API_KEY']
        else:
            logger.info(f"Using model {autogen_model_name} for AutoGen from the environment variables.")
        model_info = {'model': autogen_model_name, 'api_key': autogen_model_api_key}

        if os.getenv("AUTOGEN_MODEL_BASE_URL"):
            model_info["base_url"] = os.getenv("AUTOGEN_MODEL_BASE_URL") # type: ignore

        env_var: list[dict[str, str]] = [model_info]
        with tempfile.NamedTemporaryFile(delete=False, mode='w') as temp:
            json.dump(env_var, temp)
            temp_file_path = temp.name

        self.config_list = autogen.config_list_from_json(env_or_file=temp_file_path, filter_dict={"model": {autogen_model_name}}) # type: ignore
        self.agents_map = await self.__initialize_agents(agents_needed)
        
        def trigger_nested_chat(manager: autogen.ConversableAgent):
            print(f"Checking if nested chat should be triggered for Agent {manager}")
            content=manager.last_message()["content"] # type: ignore
            print_message_from_planner(content) # type: ignore
            return True

        def my_custom_summary_method(sender: autogen.ConversableAgent,recipient: autogen.ConversableAgent, summary_args: dict ) : # type: ignore
            print("Custom summary method called")
            last_message=recipient.last_message(sender)["content"] # type: ignore
            if not last_message or last_message.strip() == "": # type: ignore
                return "I received an empty message, try repeating."
            elif "##TERMINATE TASK##" in last_message:
                last_message=last_message.replace("##TERMINATE TASK##", "") # type: ignore
                return last_message #  type: ignore
            return recipient.last_message(sender)["content"] # type: ignore

        print(f">>> Registering nested chat. Available agents: {self.agents_map}")
        self.agents_map["user"].register_nested_chats( # type: ignore
            [
                {
            "sender": self.agents_map["browser_nav_executor"],
            "recipient": self.agents_map["browser_nav_agent"],
            "max_turns": 100,
            "summary_method": my_custom_summary_method,
                }   
            ],
            trigger=trigger_nested_chat, # type: ignore
        )

        return self


    async def __initialize_agents(self, agents_needed: list[str]):
        """
        Instantiate all agents with their appropriate prompts/skills.

        Args:
            agents_needed (list[str]): The list of agents needed, this list must have user_proxy in it or an error will be generated.

        Returns:
            dict: A dictionary of agent instances.

        """
        agents_map: dict[str, autogen.UserProxyAgent  | autogen.ConversableAgent]= {}

        user_delegate_agent = await self.__create_user_delegate_agent()
        agents_map["user"] = user_delegate_agent
        print(f"{agents_needed} agent created.")
        agents_needed.remove("user")
        
        browser_nav_executor = self.__create_browser_nav_executor_agent()
        agents_map["browser_nav_executor"] = browser_nav_executor
        agents_needed.remove("browser_nav_executor")
        
        for agent_needed in agents_needed:
            if agent_needed == "browser_nav_agent":
                browser_nav_agent: autogen.ConversableAgent = self.__create_browser_nav_agent(agents_map["browser_nav_executor"] )
                agents_map["browser_nav_agent"] = browser_nav_agent
            elif agent_needed == "planner_agent":
                planner_agent = self.__create_planner_agent()
                agents_map["planner_agent"] = planner_agent
            else:
                raise ValueError(f"Unknown agent type: {agent_needed}")
        return agents_map


    async def __create_user_delegate_agent(self) -> autogen.ConversableAgent:
        """
        Create a ConversableAgent instance.

        Returns:
            autogen.ConversableAgent: An instance of ConversableAgent.

        """
        def is_planner_termination_message(x)->bool: # type: ignore
             print(">>> Checking if planner message is a termination message:", x)
             should_terminate:bool=x.get("content", "") and x.get("content", "").rstrip().upper().endswith("##TERMINATE##") # type: ignore
             content=x.get("content", "") # type: ignore
             content=content.replace("##TERMINATE##", "").strip() # type: ignore
             if(should_terminate and content != ""): # type: ignore
                print_message_from_planner(content) # type: ignore
             print(">>> Should terminate:", should_terminate) # type: ignore
             return should_terminate # type: ignore
        
        task_delegate_agent = autogen.ConversableAgent(
            name="user",
            llm_config=False, 
            system_message=LLM_PROMPTS["USER_AGENT_PROMPT"],
            is_termination_msg=is_planner_termination_message,
            human_input_mode="NEVER",
            max_consecutive_auto_reply=self.number_of_rounds,
        )
        return task_delegate_agent

    def __create_browser_nav_executor_agent(self):
        """
        Create a UserProxyAgent instance for executing browser control.

        Returns:
            autogen.UserProxyAgent: An instance of UserProxyAgent.

        """
        browser_nav_executor_agent = autogen.UserProxyAgent(
            name="browser_nav_executor",
            is_termination_msg=lambda x: x.get("content", "") and x.get("content", "").rstrip().upper().endswith("##TERMINATE TASK##"), # type: ignore
            human_input_mode="NEVER",
            llm_config=None,
            max_consecutive_auto_reply=2,
            code_execution_config={
                                "last_n_messages": 1,
                                "work_dir": "tasks",
                                "use_docker": False,
                                },
        )
        print(">>> Created browser_nav_executor_agent:", browser_nav_executor_agent)
        print(browser_nav_executor_agent.function_map) # type: ignore
        return browser_nav_executor_agent

    def __create_browser_nav_agent(self, user_proxy_agent: autogen.UserProxyAgent) -> autogen.ConversableAgent:
        """
        Create a BrowserNavAgent instance.

        Args:
            user_proxy_agent (autogen.UserProxyAgent): The instance of UserProxyAgent that was created.

        Returns:
            autogen.AssistantAgent: An instance of BrowserNavAgent.

        """
        browser_nav_agent = BrowserNavAgent(self.config_list, user_proxy_agent) # type: ignore
        #print(">>> browser agent tools:", json.dumps(browser_nav_agent.agent.llm_config.get("tools"), indent=2))
        print(">>> browser_nav_agent:", browser_nav_agent.agent)
        return browser_nav_agent.agent

    def __create_planner_agent(self):
        """
        Create a Planner Agent instance. This is mainly used for exploration at this point

        Returns:
            autogen.AssistantAgent: An instance of PlannerAgent.

        """
        planner_agent = PlannerAgent(self.config_list) # type: ignore
        print(">>> planner_agent:", planner_agent.agent)
        return planner_agent.agent

    async def process_command(self, command: str, current_url: str | None = None) -> autogen.ChatResult | None:
        """
        Process a command by sending it to one or more agents.

        Args:
            command (str): The command to be processed.
            current_url (str, optional): The current URL of the browser. Defaults to None.

        Returns:
            autogen.ChatResult | None: The result of the command processing, or None if an error occurred. Contains chat log, cost(tokens/price)

        """
        current_url_prompt_segment = ""
        if current_url:
            current_url_prompt_segment = f"Current URL: {current_url}"

        prompt = Template(LLM_PROMPTS["COMMAND_EXECUTION_PROMPT"]).substitute(command=command, current_url_prompt_segment=current_url_prompt_segment)
        logger.info(f"Prompt for command: {prompt}")
        #with Cache.disk() as cache:
        try:
            if self.agents_map is None:
                raise ValueError("Agents map is not initialized.")
            print(f">>> agents_map: {self.agents_map}") 
            print(f">>> browser_nav_executor: {self.agents_map['browser_nav_executor']}")
            print(self.agents_map["browser_nav_executor"].function_map) # type: ignore
            
            result=await self.agents_map["user"].a_initiate_chat( # type: ignore
                self.agents_map["planner_agent"], # self.manager # type: ignore

                #clear_history=True,
                message=prompt,
                silent=False,
                cache=None,
            )
            return result
        except openai.BadRequestError as bre:
            logger.error(f"Unable to process command: \"{command}\". {bre}")
            traceback.print_exc()


    
