import { render } from "preact";
import App from "./App";
import { Companion } from "./Companion";
import { Dictation } from "./Dictation";
import { RecBadge } from "./RecBadge";
import "./styles.css";

const root = document.getElementById("root")!;
const views: Record<string, () => preact.JSX.Element> = { "#companion": () => <Companion />, "#dictation": () => <Dictation />, "#recbadge": () => <RecBadge />, "#recbadge-start": () => <RecBadge /> };
const view = (views[location.hash] ?? (() => <App />))();
render(view, root);
