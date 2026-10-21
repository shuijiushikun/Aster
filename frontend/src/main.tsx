import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { Toaster } from "./components/Toaster";
import { MetaProvider } from "./lib/meta";
import "@fontsource-variable/geist";
import "@fontsource-variable/geist-mono";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <MetaProvider>
        <Toaster>
          <App />
        </Toaster>
      </MetaProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
