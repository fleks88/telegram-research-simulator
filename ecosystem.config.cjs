const path = require("path");

const projectRoot = __dirname;
const python = path.join(projectRoot, ".venv", "bin", "python");

const common = {
  cwd: projectRoot,
  interpreter: "none",
  autorestart: true,
  restart_delay: 3000,
  max_restarts: 10,
  time: true,
  env: {
    PYTHONUNBUFFERED: "1",
  },
};

module.exports = {
  apps: [
    {
      ...common,
      name: "telegram-research-api",
      script: path.join(projectRoot, ".venv", "bin", "uvicorn"),
      args: "research_sim.api.app:app --host 127.0.0.1 --port 8000",
    },
    {
      ...common,
      name: "telegram-research-bot",
      script: python,
      args: "-m research_sim.bot.app",
    },
  ],
};