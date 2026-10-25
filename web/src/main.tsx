import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient } from "@tanstack/react-query";
import { App } from "./App";
import { makeRouter } from "./router";
import "./ui/tokens.css";
import "./ui/base.css";
import "./app.css";

const root = document.getElementById("root");
if (!root) throw new Error("#root missing from index.html");

createRoot(root).render(
  <StrictMode>
    <App router={makeRouter()} queryClient={new QueryClient()} />
  </StrictMode>,
);
