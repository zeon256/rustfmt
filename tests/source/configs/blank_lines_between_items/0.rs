// rustfmt-blank_lines_between_items: 0
// rustfmt-blank_lines_lower_bound: 1
fn one() {}
fn two() {}
struct Point {
    x: i32,
}
impl Point {
    fn a(&self) {}
    fn b(&self) {}
}
trait Shape {
    fn required(&self);
    fn optional(&self) {}
}

use std::fmt;
type Alias = Point;
