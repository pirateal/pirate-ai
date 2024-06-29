import os
import logging
import sqlite3
import json
import subprocess
import re
import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread
from datetime import datetime

# Configure structured logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load main configuration
try:
    with open('config.json', 'r') as config_file:
        main_config = json.load(config_file)
except Exception as e:
    logger.error(f"Error loading config.json: {e}")
    exit(1)

LOCAL_API_ENDPOINT = os.getenv('LOCAL_API_ENDPOINT', main_config.get('LOCAL_API_ENDPOINT'))
WORKING_DIRECTORY = os.getenv('WORKING_DIRECTORY', main_config.get('WORKING_DIRECTORY'))
MODEL = main_config.get('model', 'default-model')

# Ensure the working directory exists
os.makedirs(WORKING_DIRECTORY, exist_ok=True)
logger.info(f"Using working directory: {WORKING_DIRECTORY}")

# Database connection setup
DATABASE_PATH = os.path.join(WORKING_DIRECTORY, "agent_memory.db")
conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS memory 
             (id INTEGER PRIMARY KEY, agent TEXT, user_input TEXT, ai_response TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
conn.commit()
logger.info(f"Database setup completed at: {DATABASE_PATH}")

class Agent:
    def __init__(self, name: str, system_message: str):
        self.name = name
        self.system_message = system_message
        self.chat_history = [{"role": "system", "content": self.system_message}]
        self.current_task = ""

    def update_system_message(self, new_system_message: str):
        self.system_message = new_system_message
        self.chat_history[0] = {"role": "system", "content": self.system_message}

    async def generate_reply(self, user_input: str) -> str:
        self.chat_history.append({"role": "user", "content": user_input})
        self.current_task = user_input

        try:
            self.prune_chat_history()
            async with aiohttp.ClientSession() as session:
                async with session.post(LOCAL_API_ENDPOINT, json={"model": MODEL, "messages": self.chat_history}) as response:
                    data = await response.json()
                    ai_message = data["choices"][0]["message"]["content"]
                    self.chat_history.append({"role": "assistant", "content": ai_message})
                    return ai_message
        except aiohttp.ClientConnectorError as e:
            logger.error(f"Error generating reply: {e}")
            return "Failed to connect to the API endpoint."
        except Exception as e:
            logger.error(f"Unexpected error generating reply: {e}")
            return "An unexpected error occurred during response generation."

    def prune_chat_history(self):
        total_tokens = sum(len(message['content']) for message in self.chat_history)
        while total_tokens > 2000:
            self.chat_history.pop(1)
            total_tokens = sum(len(message['content']) for message in self.chat_history)

    def save_to_memory(self, user_input: str, ai_response: str):
        c.execute("INSERT INTO memory (agent, user_input, ai_response) VALUES (?, ?, ?)", (self.name, user_input, ai_response))
        conn.commit()
        return c.lastrowid

    def get_relevant_memory(self, user_input: str) -> list:
        c.execute("SELECT agent, user_input, ai_response, timestamp FROM memory WHERE user_input LIKE ? ORDER BY timestamp DESC LIMIT 5", (f"%{user_input}%",))
        return [{"agent": row[0], "user_input": row[1], "ai_response": row[2], "timestamp": row[3]} for row in c.fetchall()]

    def execute_command(self, command: str) -> str:
        try:
            result = subprocess.run(command, shell=True, check=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            return result.stdout.strip() or "Command executed successfully with no output."
        except subprocess.CalledProcessError as e:
            error_message = e.stdout.strip()
            logger.error(f"Command execution failed: {error_message}")
            if 'permission denied' in error_message:
                return "Permission denied. Try running the command with elevated privileges."
            elif 'not found' in error_message:
                return f"Command not found. Ensure the command is typed correctly and try again."
            else:
                return f"An error occurred: {error_message}"
        except Exception as e:
            logger.error(f"Unexpected error: {str(e)}")
            return f"An unexpected error occurred: {str(e)}"

class SupervisorAgent:
    def __init__(self):
        self.agents = {}

    def add_agent(self, agent: Agent):
        self.agents[agent.name] = agent

    async def delegate_task(self, user_input: str) -> str:
        logger.info(f"Supervisor delegating task: {user_input}")
        agent_name = f"agent_{len(self.agents) + 1}"
        agent = Agent(agent_name, "You are a versatile agent capable of executing various tasks.")
        self.add_agent(agent)
        
        if user_input.startswith('run command'):
            command = user_input[len('run command '):].strip()
            response = agent.execute_command(command)
        else:
            response = await agent.generate_reply(user_input)
        
        task_id = agent.save_to_memory(user_input, response)
        return response, task_id

async def execute_task(user_input: str):
    result, task_id = await supervisor.delegate_task(user_input)
    save_test_result(user_input, result)
    print(f"\nTask: {user_input}\nResult:\n{result}\nTask ID: {task_id}\n")

def save_test_result(user_input: str, result: str):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    file_name = f"test_result_{timestamp}.txt"
    file_path = os.path.join(WORKING_DIRECTORY, file_name)
    with open(file_path, 'w') as file:
        file.write(f"Task: {user_input}\nResult:\n{result}\n")

def load_tests(file_name: str) -> list:
    with open(file_name, 'r') as file:
        return [line.strip() for line in file.readlines()]

# Create supervisor
supervisor = SupervisorAgent()

# Queue and worker for background task processing
task_queue = Queue()

def worker():
    while True:
        user_input = task_queue.get()
        if user_input is None:
            break
        asyncio.run(execute_task(user_input))
        task_queue.task_done()

async def main():
    logger.info("Intelligent Programming Assistant started")

    print("\n=== Intelligent Programming Assistant ===")
    print("Enter your task. Type 'help' for a list of commands. Type 'quit' to exit.\n")

    def display_help():
        help_text = """
        Available Commands:
        - help: Display this help menu.
        - quit: Exit the program.
        - run tests: Start running predefined tests from 'test_tasks.txt'.
        - [any other command]: The agent will attempt to perform the task.
        """
        print(help_text)

    worker_thread = Thread(target=worker)
    worker_thread.start()

    try:
        while True:
            user_input = input(">> ").strip().lower()
            if user_input == 'quit':
                break
            elif user_input == 'help':
                display_help()
            elif user_input == 'run tests':
                tests = load_tests('test_tasks.txt')
                for test in tests:
                    task_queue.put(test)
            else:
                task_queue.put(user_input)
    finally:
        task_queue.put(None)
        worker_thread.join()

    logger.info("Intelligent Programming Assistant stopped")

if __name__ == "__main__":
    asyncio.run(main())
