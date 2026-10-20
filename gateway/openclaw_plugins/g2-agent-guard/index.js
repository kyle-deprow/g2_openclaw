import { PLUGIN_ID, decide, validateConfig } from "./guard.js";

export { decide };

export default {
  id: PLUGIN_ID,
  name: "G2 Agent Guard",
  register(api) {
    const config = api.pluginConfig;
    const invalid = validateConfig(config);
    if (invalid) console.error(`${PLUGIN_ID}: invalid config (${invalid}); every tool call will be blocked`);
    api.on("before_tool_call", (event, ctx) => decide(config, event?.toolName, event?.params, ctx?.agentId) ?? undefined);
  },
};
