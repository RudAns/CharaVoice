import { render } from "preact";
import App from "./App";
import { Companion } from "./Companion";
import { Dictation } from "./Dictation";
import "./styles.css";

const root = document.getElementById("root")!;
const view = location.hash === "#companion" ? <Companion /> : location.hash === "#dictation" ? <Dictation /> : <App />;
render(view, root);
