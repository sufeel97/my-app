import { createInitialState, setDirection, stepGame } from "./game.js";

const TICK_MS = 140;
const board = document.querySelector("#board");
const scoreValue = document.querySelector("#score");
const bestScoreValue = document.querySelector("#best-score");
const statusText = document.querySelector("#status");
const restartButton = document.querySelector("#restart-button");
const controlButtons = document.querySelectorAll("[data-direction]");

let state = createInitialState();
let tickHandle = null;

function buildBoard() {
  const cells = [];

  for (let index = 0; index < state.gridSize * state.gridSize; index += 1) {
    const cell = document.createElement("div");
    cell.className = "cell";
    board.append(cell);
    cells.push(cell);
  }

  return cells;
}

const cells = buildBoard();

function render() {
  for (const cell of cells) {
    cell.className = "cell";
  }

  for (const segment of state.snake) {
    const index = segment.y * state.gridSize + segment.x;
    cells[index]?.classList.add("snake");
  }

  if (state.food) {
    const foodIndex = state.food.y * state.gridSize + state.food.x;
    cells[foodIndex]?.classList.add("food");
  }

  scoreValue.textContent = String(state.score);
  bestScoreValue.textContent = String(state.bestScore);

  if (state.isGameOver) {
    statusText.textContent = "Game over. Press Restart to play again.";
    stopLoop();
  } else if (!state.hasStarted) {
    statusText.textContent = "Press any arrow key or WASD to start.";
  } else {
    statusText.textContent = "Running.";
  }
}

function tick() {
  state = stepGame(state);
  render();
}

function startLoop() {
  if (tickHandle !== null) {
    return;
  }

  tickHandle = window.setInterval(tick, TICK_MS);
}

function stopLoop() {
  if (tickHandle === null) {
    return;
  }

  window.clearInterval(tickHandle);
  tickHandle = null;
}

function queueDirection(direction) {
  const nextState = setDirection(state, direction);
  const hasJustStarted = !state.hasStarted && nextState.hasStarted;
  state = nextState;
  if (hasJustStarted) {
    startLoop();
  }
  render();
}

function restartGame() {
  const bestScore = state.bestScore;
  stopLoop();
  state = createInitialState();
  state.bestScore = bestScore;
  render();
}

const KEY_TO_DIRECTION = {
  ArrowUp: "up",
  ArrowDown: "down",
  ArrowLeft: "left",
  ArrowRight: "right",
  w: "up",
  a: "left",
  s: "down",
  d: "right",
  W: "up",
  A: "left",
  S: "down",
  D: "right"
};

window.addEventListener("keydown", (event) => {
  const direction = KEY_TO_DIRECTION[event.key];
  if (!direction) {
    return;
  }

  event.preventDefault();
  queueDirection(direction);
});

restartButton.addEventListener("click", restartGame);

for (const button of controlButtons) {
  button.addEventListener("click", () => {
    queueDirection(button.dataset.direction);
  });
}

render();
