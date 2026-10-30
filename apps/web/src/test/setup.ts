/// <reference types="vitest" />
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// 非 globals 模式下 testing-library 不自动清理，必须显式接上
afterEach(cleanup);
