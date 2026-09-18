import json
from .config import Config

def main():
    c=Config.from_env()
    print(json.dumps({"magi3":"foundation","mode":c.mode,"live_enabled":c.live_enabled,"kill_switch":c.kill_switch,"can_submit_live":c.can_submit_live}))

if __name__=="__main__":main()
