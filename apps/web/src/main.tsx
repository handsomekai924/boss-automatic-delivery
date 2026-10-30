import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ThemeModeProvider } from "./theme/ThemeModeProvider";
import { ToastProvider } from "./components/feedback/Toast";
import { App } from "./App";
import "./theme/global.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeModeProvider>
      <ToastProvider>
        <App />
      </ToastProvider>
    </ThemeModeProvider>
  </StrictMode>,
);
