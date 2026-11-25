// ESLint for the web console (app/web/, browser ES modules, no build step).
// Run: npm ci && npm run lint (repo-kit CI standard, category 2).
import js from "@eslint/js";
import globals from "globals";

export default [
  {
    files: ["app/web/**/*.js"],
    ...js.configs.recommended,
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser },
    },
    rules: {
      ...js.configs.recommended.rules,
      // `catch (_error)` without using the error is the console's convention for
      // "the request failed, show the generic message".
      "no-unused-vars": ["error", { caughtErrors: "none" }],
    },
  },
  // Baseline when ESLint was introduced (#14): findings in existing console code are
  // reported as warnings so the device code was not changed by the CI change. Remove an
  // entry when the file is next changed for its own reason and fix the finding with it.
  {
    // init is imported but unused; settingForm() and clearFormStatus() are unused helpers.
    files: ["app/web/common.js"],
    rules: { "no-unused-vars": ["warn", { caughtErrors: "none" }] },
  },
  {
    // An initial value that every branch overwrites (live.js `message`, timelapse.js `ok`).
    files: ["app/web/live.js", "app/web/timelapse.js"],
    rules: { "no-useless-assignment": "warn" },
  },
];
