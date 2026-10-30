import { Component, type ErrorInfo, type ReactNode } from "react";
import { log } from "../lib/logger";

export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: Error, _info: ErrorInfo) { log.error("ui_crash", { error_type: error.name }); }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="auth-form" style={{ minHeight: "100%" }}>
        <div className="consent"><h1>Something went wrong</h1><p className="muted">The page hit an unexpected problem. Your data is safe. Reload the page to continue.</p>
          <button className="btn btn-primary" onClick={() => location.reload()}>Reload</button></div>
      </div>
    );
  }
}
