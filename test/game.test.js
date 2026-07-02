import test from "node:test";
import assert from "node:assert/strict";

import { createInitialState, placeFood, setDirection, stepGame } from "../src/game.js";

test("movement advances the snake in the current direction", () => {
  let state = createInitialState(() => 0);
  state = setDirection(state, "right");
  state = stepGame(state, () => 0);

  assert.deepEqual(state.snake[0], { x: 3, y: 8 });
  assert.equal(state.score, 0);
  assert.equal(state.isGameOver, false);
});

test("snake grows and score increases when it eats food", () => {
  let state = createInitialState(() => 0);
  state.food = { x: 3, y: 8 };
  state = setDirection(state, "right");
  state = stepGame(state, () => 0);

  assert.equal(state.snake.length, 4);
  assert.equal(state.score, 1);
  assert.notDeepEqual(state.food, { x: 3, y: 8 });
});

test("reversing direction is ignored", () => {
  let state = createInitialState(() => 0);
  state = setDirection(state, "left");

  assert.equal(state.pendingDirection, "right");
});

test("wall collisions end the game", () => {
  let state = createInitialState(() => 0);
  state.snake = [{ x: 15, y: 0 }];
  state.direction = "right";
  state.pendingDirection = "right";
  state.hasStarted = true;

  state = stepGame(state, () => 0);

  assert.equal(state.isGameOver, true);
});

test("self collisions end the game", () => {
  let state = createInitialState(() => 0);
  state.snake = [
    { x: 3, y: 2 },
    { x: 3, y: 3 },
    { x: 2, y: 3 },
    { x: 2, y: 2 }
  ];
  state.direction = "left";
  state.pendingDirection = "down";
  state.hasStarted = true;

  state = stepGame(state, () => 0);

  assert.equal(state.isGameOver, true);
});

test("food placement never uses an occupied cell", () => {
  const snake = [
    { x: 0, y: 0 },
    { x: 1, y: 0 },
    { x: 0, y: 1 }
  ];

  const food = placeFood(snake, 3, () => 0);

  assert.deepEqual(food, { x: 2, y: 0 });
});
