import json
import sys
sys.stdout.reconfigure(encoding='utf-8')
with open('run_output.json', 'r', encoding='utf-8') as f:
    d = json.load(f)
for r in d['results'][:20]:
    print(f"Q: {r['question']}\nA: {r['answer']}\n")
