import sys
sys.path.insert(0, ".")
import coaching_tester as ct
import vi_agent

vi_agent.configure_client("mens_fashion_alto")
vi_agent.load_environment()
ct._install_coaching_playbook_stub()
system_instruction = ct._build_system_instruction_with_coaching()
chat = vi_agent.build_chat(system_instruction=system_instruction)

tool_calls_log = []
question = "El vendedor Juan tiene una tasa muy baja en sugerir productos complementarios. ¿Qué le recomendarías para mejorar?"
answer = vi_agent.run_tool_loop(chat, question, max_tool_calls=20, debug=False, tool_calls_log=tool_calls_log)

print("===== TOOL CALLS =====")
for call in tool_calls_log:
    print("name:", call["name"], "args:", call["args"])
    print("result (primeros 400 chars):", str(call["result"])[:400])
print()
print("===== RESPUESTA =====")
print(answer)
