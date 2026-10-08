package io.mimicbrowser.sdk;

/** Explicit Context identity available before the application's first page. */
public record ContextSetup(String browserContextId, MimicClient mimic) {}
