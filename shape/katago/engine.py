import copy
import json
import logging
import os
import queue
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from shape.utils import setup_logging

logger = setup_logging()


class KataGoEngine:
    def __init__(self, katago_path):
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        config_path = os.path.join(base_dir, "models", "analysis.cfg")
        model_path = os.path.join(base_dir, "models", "katago-28b.bin.gz")
        human_model_path = os.path.join(base_dir, "models", "katago-human.bin.gz")
        if not os.path.exists(config_path) or not os.path.exists(model_path) or not os.path.exists(human_model_path):
            raise RuntimeError("Models not found. Run install.sh to download the models.")

        command = [
            os.path.abspath(katago_path),
            "analysis",
            "-config",
            config_path,
            "-model",
            model_path,
            "-human-model",
            human_model_path,
        ]

        self.process = self._start_process(command)
        if self.process.poll() is not None:
            self._handle_process_exit()

        self.query_queue = queue.Queue()
        self.response_callbacks = {}

        threads = [
            threading.Thread(target=self._log_stderr, daemon=True),
            threading.Thread(target=self._process_responses, daemon=True),
            threading.Thread(target=self._process_query_queue, daemon=True),
        ]
        for thread in threads:
            thread.start()

        self.query_counter = 0  # Initialize a counter for query IDs

    def _start_process(self, command):
        try:
            return subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
        except Exception as e:
            logger.error(f"Failed to start KataGo process: {e}")
            raise

    def _handle_process_exit(self):
        error_output = self.process.stderr.read() if self.process.stderr else "No error output available"
        logger.error(f"KataGo process exited unexpectedly. Error output: {error_output}")
        raise RuntimeError("KataGo process exited unexpectedly")

    def _log_stderr(self):
        if self.process.stderr:
            for line in self.process.stderr:
                logger.info(f"[KataGo] {line.strip()}")

    def _process_responses(self):
        if self.process.stdout:
            for line in self.process.stdout:
                try:
                    response = json.loads(line)
                    query_id = response.get("id")
                    if query_id and query_id in self.response_callbacks:
                        callback = self.response_callbacks.pop(query_id)
                        self._log_response(response)
                        logger.debug(f"Calling callback for query_id: {query_id}")
                        try:
                            callback(response)
                        except Exception as e:
                            logger.error(f"Error calling callback for query_id: {query_id}, error: {e}")
                            traceback.print_exc()
                        logger.debug(f"Callback called for query_id: {query_id}")
                    else:
                        logger.error(f"Received response with unknown id: {query_id}")
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse KataGo response: {line.strip()}")

    def _log_response(self, response):
        response = copy.deepcopy(response)
        for k in ["policy", "humanPolicy"]:
            if k in response:
                response[k] = f"[{len(response[k])} floats]"
        moves = [
            {k: v for k, v in move.items() if k in ["move", "visits", "winrate"]}
            for move in response.get("moveInfos", [])
        ]
        response["moveInfos"] = moves[:5] + [f"{len(moves)-5} more..."] if len(moves) > 5 else moves
        logger.debug(f"Received response: {json.dumps(response, indent=2)}")

    def analyze_position(self, moves, callback, rules, human_profile_settings, max_visits=100):
        self.query_counter += 1
        query_id = f"{len(moves)}_{''.join(moves[-1]) if moves else 'root'}_{human_profile_settings.get('humanSLProfile','ai')}_{max_visits}v_{self.query_counter}"
        query = {
            "id": query_id,
            "moves": moves,
            **rules,
            "includePolicy": True,
            "includeOwnership": False,
            "maxVisits": max_visits,
            "overrideSettings": human_profile_settings,
        }
        self.query_queue.put((query, callback))

    def _process_query_queue(self):
        while True:
            query, callback = self.query_queue.get()
            if query is None:
                break
            try:
                if self.process.stdin:
                    logger.debug(f"Sending query: {json.dumps(query, indent=2)}")
                    self.process.stdin.write(json.dumps(query) + "\n")
                    self.process.stdin.flush()
                    logger.debug(f"Sent query id {query['id']}")
                self.response_callbacks[query["id"]] = callback
            except Exception as e:
                logger.error(f"Error sending query: {e}")
                callback({"error": str(e)})
            self.query_queue.task_done()

    def close(self):
        self.query_queue.put((None, None))
        if self.process:
            self.process.terminate()
            self.process.wait()