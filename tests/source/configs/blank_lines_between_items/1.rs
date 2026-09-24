// rustfmt-blank_lines_between_items: 1
fn one() {}

fn two() {}

struct Point {
    x: i32,
}

impl Point {
    fn a(&self) {}
    const N: usize = 1;

    fn b(&self) {}
}

trait Shape {
    fn required(&self);

    fn optional(&self) {}

    // Leading comment on its own member.
    fn extra(&self) {}
}

enum Color {
    Red,
    Green,
}

union Pair {
    a: i32,
    b: f32,
}

type Alias = Point;

fn locals() {
    struct Local;

    fn inner() {}
}

use std::fmt;

/// Doc comment stays attached.
fn documented() {}

#[allow(dead_code)]
fn attributed() {}

// Keep this comment attached to the impl.
impl Shape for Point {
    fn required(&self) {}
}

mod decl_style {
    fn a() {}

    fn b() {}
}

#[rustfmt::skip]
fn untouched() {}

fn still_spaced_after_skip() {}
