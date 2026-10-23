import { render } from "preact";
import App from "./App";
import { Companion } from "./Companion";
import "./styles.css";

const root = document.getElementById("root")!;
render(location.hash === "#companion" ? <Companion /> : <App />, root);
