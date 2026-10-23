#![cfg(target_os = "linux")]

use glib::variant::ToVariant;

#[test]
fn variant_string_iterator_reads_forward_backward_and_skips() {
    let values = ["first", "", "Unicode: λ", "last"];
    let variant = values.to_variant();
    assert_eq!(
        variant.array_iter_str().unwrap().collect::<Vec<_>>(),
        values
    );
    assert_eq!(
        variant.array_iter_str().unwrap().rev().collect::<Vec<_>>(),
        values.into_iter().rev().collect::<Vec<_>>()
    );

    let mut iter = variant.array_iter_str().unwrap();
    assert_eq!(iter.next(), Some("first"));
    assert_eq!(iter.next_back(), Some("last"));
    assert_eq!(iter.nth(1), Some("Unicode: λ"));
    assert_eq!(iter.next(), None);

    assert_eq!(
        variant.array_iter_str().unwrap().nth_back(1),
        Some("Unicode: λ")
    );
    assert_eq!(variant.array_iter_str().unwrap().last(), Some("last"));

    let empty = Vec::<&str>::new().to_variant();
    assert_eq!(empty.array_iter_str().unwrap().next(), None);
}
